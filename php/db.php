<?php
declare(strict_types=1);

/**
 * db.php — returns a connected PDO to PostgreSQL.
 */
function db(): PDO {
    static $pdo = null;
    if ($pdo instanceof PDO) {
        return $pdo;
    }

    $cfg = require __DIR__ . '/config.php';

    $dsn = $cfg['pg_dsn'];
    if ($dsn) {
        // Accept postgresql://user:pass@host:port/dbname
        if (str_starts_with($dsn, 'postgresql://')) {
            $parts = parse_url($dsn);
            $host = $parts['host'] ?? 'localhost';
            $port = isset($parts['port']) ? (int)$parts['port'] : 5432;
            $user = $parts['user'] ?? null;
            $pass = $parts['pass'] ?? null;
            $dbname = ltrim($parts['path'] ?? '', '/');
            $pdo = new PDO("pgsql:host=$host;port=$port;dbname=$dbname", $user, $pass, $cfg['pdo_options']);
            return $pdo;
        }
        // Or native pgsql:host=...;port=...;dbname=... DSN
        $pdo = new PDO($dsn, null, null, $cfg['pdo_options']);
        return $pdo;
    }

    // Fallback to libpq env vars
    $pdo = new PDO(
        sprintf('pgsql:host=%s;port=%d;dbname=%s', $cfg['pg_host'], $cfg['pg_port'], $cfg['pg_db']),
        $cfg['pg_user'],
        $cfg['pg_pass'],
        $cfg['pdo_options']
    );
    return $pdo;
}
