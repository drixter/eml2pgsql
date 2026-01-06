<?php
declare(strict_types=1);

/**
 * Centralized configuration for DB access.
 * Preferred: set PG_DSN env var (postgresql://user:pass@host:port/dbname)
 * Fallback: PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE env vars.
 */

function env(string $key, ?string $default = null): ?string {
    $val = $_ENV[$key] ?? getenv($key);
    return ($val === false) ? $default : $val;
}

return [
    // If PG_DSN is set, it will be used. Example: postgresql://user:pass@localhost:5432/maildb
    'pg_dsn' => env('PG_DSN'),

    // Fallback libpq-style envs (used when pg_dsn is empty)
    'pg_host' => env('PGHOST', 'localhost'),
    'pg_port' => (int)env('PGPORT', '5432'),
    'pg_user' => env('PGUSER', 'xxx'),
    'pg_pass' => env('PGPASSWORD', 'xxx'),
    'pg_db'   => env('PGDATABASE', 'xx'),

    // PDO options
    'pdo_options' => [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
    ],
];
