<?php

$sf = __DIR__ . '/status.json';
$qf = __DIR__ . '/queue.json';

if (!file_exists($sf)) {
    file_put_contents($sf, json_encode([
        "processing" => null,
        "completed" => []
    ]));
}

if (!file_exists($qf)) {
    file_put_contents($qf, json_encode([]));
}

$data = json_decode(file_get_contents($sf), true);
$queue = json_decode(file_get_contents($qf), true);

if (!is_array($data)) {
    $data = ["processing" => null, "completed" => []];
}

if (!is_array($queue) || empty($queue)) exit;

$url = array_shift($queue);

$data['processing'] = [
    "url" => $url,
    "status" => "Downloading..."
];

file_put_contents($qf, json_encode($queue));
file_put_contents($sf, json_encode($data));

function saveStatus($data, $sf)
{
    file_put_contents($sf, json_encode($data));
}

function fail($data, $sf, $url, $msg)
{
    $data['completed'][] = [
        "url" => $url,
        "success" => false,
        "log" => $msg
    ];
    $data['processing'] = null;
    saveStatus($data, $sf);
    exit;
}

$inputDir = __DIR__ . '/input_music';
$musicDir = __DIR__ . '/storage/music';
$thumbDir = __DIR__ . '/storage/thumbnails';

@mkdir($inputDir, 0777, true);
@mkdir($musicDir, 0777, true);
@mkdir($thumbDir, 0777, true);

exec(
    'yt-dlp --no-part -x --audio-format mp3 --embed-metadata --write-thumbnail --convert-thumbnails jpg -o "' . $inputDir . '/%(title)s.%(ext)s" ' . escapeshellarg($url) . ' 2>&1',
    $o1,
    $s1
);

if ($s1 !== 0) {
    fail($data, $sf, $url, implode("\n", $o1));
}

$data['processing']['status'] = "Python starting...";
saveStatus($data, $sf);

putenv("DB_HOST=localhost");
putenv("DB_PORT=3306");
putenv("DB_USER=root");
putenv("DB_PASSWORD=");
putenv("DB_NAME=music_app_v2");

putenv("INPUT_DIR=" . $inputDir);
putenv("MUSIC_DIR=" . $musicDir);
putenv("THUMB_DIR=" . $thumbDir);

$cmd = 'python "' . __DIR__ . '/ingest_music_from_folder.py"';

$descriptorspec = [
    0 => ["pipe", "r"],
    1 => ["pipe", "w"],
    2 => ["pipe", "w"]
];

$process = proc_open($cmd, $descriptorspec, $pipes);

if (!is_resource($process)) {
    fail($data, $sf, $url, "Failed to start python");
}

stream_set_blocking($pipes[1], false);
stream_set_blocking($pipes[2], false);

$log = '';

while (true) {

    $status = proc_get_status($process);

    $out = stream_get_contents($pipes[1]);
    $err = stream_get_contents($pipes[2]);

    if ($out) $log .= $out;
    if ($err) $log .= $err;

    $data['processing']['status'] = $log ?: "Running...";
    saveStatus($data, $sf);

    if (!$status['running']) break;

    usleep(500000);
}

$code = proc_close($process);

if ($code !== 0) {
    fail($data, $sf, $url, $log ?: "Python failed");
}

$data['completed'][] = [
    "url" => $url,
    "success" => true,
    "log" => $log ?: "Imported successfully"
];

$data['processing'] = null;

saveStatus($data, $sf);
