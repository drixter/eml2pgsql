<?php
declare(strict_types=1);
require_once __DIR__ . '/db.php';

function h($s){ return htmlspecialchars((string)$s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8'); }

$pdo = db();
header('Content-Type: text/html; charset=utf-8');

echo "<h1>FTS Health Check</h1>";

echo "<h2>Columns</h2>";
$stmt = $pdo->query("SELECT column_name, data_type
                     FROM information_schema.columns
                     WHERE table_schema = 'mail' AND table_name = 'emails'
                     ORDER BY ordinal_position");
echo "<pre>";
foreach ($stmt->fetchAll() as $r) {
    echo h($r['column_name']) . " : " . h($r['data_type']) . "\n";
}
echo "</pre>";

echo "<h2>Indexes</h2>";
$stmt = $pdo->query("SELECT indexname, indexdef
                     FROM pg_indexes
                     WHERE schemaname = 'mail' AND tablename = 'emails'");
echo "<pre>";
foreach ($stmt->fetchAll() as $r) {
    echo h($r['indexname']) . " => " . h($r['indexdef']) . "\n";
}
echo "</pre>";

echo "<h2>Sample FTS query (plainto_tsquery)</h2>";
$q = "invoice email error"; // sample user query
$sql = "EXPLAIN ANALYZE
        SELECT id, subject, ts_rank(fts, plainto_tsquery('simple', :q)) AS rank
        FROM mail.emails
        WHERE fts @@ plainto_tsquery('simple', :q)
        ORDER BY rank DESC
        LIMIT 20";
$stmt = $pdo->prepare($sql);
$stmt->bindValue(':q', $q);
$stmt->execute();
echo "<pre>" . h(implode("\n", array_map(fn($r) => $r[0], $stmt->fetchAll(PDO::FETCH_NUM)))) . "</pre>";

echo "<p><em>Look for 'Bitmap Index Scan on idx_emails_fts' or 'Index Cond' showing use of the GIN index.</em></p>";

echo "<h2>Sample Headline</h2>";
$sql2 = "SELECT ts_headline('simple', text_body, plainto_tsquery('simple', :q),
                            'HighlightAll=true, StartSel=&lt;mark&gt;, StopSel=&lt;/mark&gt;') AS snippet
         FROM mail.emails
         WHERE fts @@ plainto_tsquery('simple', :q)
         LIMIT 3";
$stmt2 = $pdo->prepare($sql2);
$stmt2->bindValue(':q', $q);
$stmt2->execute();
echo "<div style='border:1px solid #ccc;padding:8px'>";
foreach ($stmt2->fetchAll() as $r) {
    echo "<div>" . ($r['snippet'] ?: '<em>(no snippet)</em>') . "</div><hr>";
}
echo "</div>";
