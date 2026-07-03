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

if ($_SERVER['REQUEST_METHOD'] === 'GET') {
    $status = json_decode(file_get_contents($sf), true);
    if (!is_array($status)) {
        $status = ["processing" => null, "completed" => []];
    }
    $status['completed'] = [];
    file_put_contents($sf, json_encode($status));
}

if ($_SERVER['REQUEST_METHOD'] === 'POST') {

    $url = trim($_POST['youtube_url']);

    if ($url) {
        $queue = json_decode(file_get_contents($qf), true);
        if (!is_array($queue)) {
            $queue = [];
        }

        $queue[] = $url;
        file_put_contents($qf, json_encode($queue));
    }

    exit;
}
?>

<!DOCTYPE html>
<html>

<head>
    <title>Music Dashboard</title>

    <style>
        body {
            font-family: Arial;
            background: #0f172a;
            color: white;
            padding: 40px;
            max-width: 1300px;
            margin: auto;
        }

        .card {
            background: #1e293b;
            padding: 25px;
            border-radius: 18px;
            margin-bottom: 20px;
        }

        input {
            width: 100%;
            padding: 18px;
            border: none;
            border-radius: 12px;
        }

        button {
            margin-top: 14px;
            width: 100%;
            padding: 16px;
            border: none;
            border-radius: 12px;
            background: #3b82f6;
            color: white;
            cursor: pointer;
        }

        .item {
            padding: 14px;
            margin: 10px 0;
            border-radius: 12px;
            word-break: break-all;
        }

        .queue {
            background: #334155;
        }

        .processing {
            background: #f59e0b;
        }

        .success {
            background: #16a34a;
        }

        .fail {
            background: #dc2626;
        }

        pre {
            white-space: pre-wrap;
            margin-top: 10px;
            font-size: 13px;
        }
    </style>
</head>

<body>

    <div class="card">
        <h1>🎵 Music Import Dashboard</h1>

        <form id="f">
            <input id="u" name="youtube_url" placeholder="Paste YouTube URL">
            <button>Add To Queue</button>
        </form>
    </div>

    <div class="card">
        <h2>Queue</h2>
        <div id="queue"></div>
    </div>

    <div class="card">
        <h2>Under Process</h2>
        <div id="processing"></div>
    </div>

    <div class="card">
        <h2>Completed</h2>
        <div id="completed"></div>
    </div>

    <script>
        document.getElementById('f').onsubmit = async e => {

            e.preventDefault();

            let fd = new FormData(e.target);

            await fetch('add_music.php', {
                method: 'POST',
                body: fd
            });

            document.getElementById('u').value = '';

            load();
        };

        async function load() {

            let r = await fetch('status_api.php');
            let d = await r.json();

            document.getElementById('queue').innerHTML =
                d.queue.map(x => `<div class="item queue">${x}</div>`).join('');

            document.getElementById('processing').innerHTML =
                d.processing ?
                `<div class="item processing">${d.processing.url}<pre>${d.processing.status}</pre></div>` :
                'Idle';

            document.getElementById('completed').innerHTML =
                d.completed.slice().reverse().map(x =>
                    `<div class="item ${x.success?'success':'fail'}">
${x.url}
<pre>${x.log}</pre>
</div>`
                ).join('');
        }

        setInterval(load, 2000);
        load();
    </script>

</body>

</html>