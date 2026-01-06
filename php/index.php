<?php
declare(strict_types=1);
require_once __DIR__ . '/db.php';

/**
 * Mail Viewer with Full-Text Search (FTS)
 * Schema: mail.emails (includes: fts tsvector), mail.attachments
 *
 * Routes (?action=...):
 *  - list (default): list emails, FTS with ranking + pagination
 *  - view&id=EMAIL_ID: email details + attachments
 *  - download_attachment&id=ATTACH_ID: download attachment (BYTEA)
 *  - cid&email_id=EMAIL_ID&cid=CID: serve inline CID resources (images, etc.)
 */

function h(?string $s): string {
    return htmlspecialchars($s ?? '', ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}
function param(string $name, ?string $default = null): ?string {
    return isset($_GET[$name]) ? (string)$_GET[$name] : $default;
}
function postv(string $name, ?string $default = null): ?string {
    return isset($_POST[$name]) ? (string)$_POST[$name] : $default;
}
function normalize_cid(?string $cid): ?string {
    if ($cid === null) return null;
    $cid = trim($cid);
    if ($cid !== '' && $cid[0] === '<' && substr($cid, -1) === '>') {
        $cid = substr($cid, 1, -1);
    }
    return $cid;
}

function header_html(string $title): void {
    echo '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">';
    echo '<title>' . h($title) . '</title>';
    echo '<style>
        body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#f7f7f9;color:#222;}
        header{background:#1f2937;color:#fff;padding:12px 16px;}
        main{padding:16px;max-width:1100px;margin:0 auto;}
        a{color:#0a58ca;text-decoration:none;} a:hover{text-decoration:underline;}
        .toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap;}
        input[type=text]{padding:6px 8px;border:1px solid #ccc;border-radius:6px;min-width:280px;}
        .btn{padding:6px 10px;border:1px solid #888;border-radius:6px;background:#fff;cursor:pointer;}
        .btn-primary{background:#0d6efd;color:#fff;border-color:#0d6efd;}
        table{width:100%;border-collapse:collapse;background:#fff;}
        th,td{padding:8px;border-bottom:1px solid #e5e7eb;vertical-align:top;}
        th{background:#f3f4f6;text-align:left;}
        .muted{color:#555;}
        .pill{display:inline-block;padding:2px 6px;border:1px solid #ddd;border-radius:999px;background:#fafafa;font-size:12px;margin-left:6px;}
        .card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:12px;}
        pre{white-space:pre-wrap;word-wrap:break-word;}
        .row{display:flex;gap:12px;flex-wrap:wrap;}
        .col{flex:1 1 48%;}
        .nowrap{white-space:nowrap;}
        .attachments li{margin-bottom:6px;}
        .footer{margin-top:40px;color:#666;font-size:12px;}
    </style></head><body><header><strong>Mail Viewer</strong></header><main>';
}
function footer_html(): void {
    echo '<div class="footer">Mail Viewer • PHP + PostgreSQL (FTS)</div></main></body></html>';
}

/**
 * LIST: FTS search with ranking and pagination
 */
function action_list(): void {
    $pdo = db();

    $q = trim((string)param('q', ''));
    $page = max(1, (int)param('page', '1'));
    $pageSize = max(1, min(100, (int)param('page_size', '50')));
    $offset = ($page - 1) * $pageSize;

    $useFts = ($q !== '');
    $showSnippet = (param('snippet', '0') === '1');
    $advanced = (param('advanced', '0') === '1'); // to_tsquery when enabled

    if ($useFts) {
        if ($advanced) {
            // Count with to_tsquery (expects tsquery syntax: OR '|', adjacency '<->', prefix ':*')
            $stmt = $pdo->prepare("SELECT COUNT(*) AS c
                                   FROM mail.emails
                                   WHERE fts @@ to_tsquery('simple', :q)");
            $stmt->bindValue(':q', $q);
            $stmt->execute();
            $total = (int)$stmt->fetch()['c'];

            // Page data with rank (and optional snippet)
            if ($showSnippet) {
                $sql = "SELECT id, subject, from_addr, to_addrs, sent_at_utc, message_id,
                               ts_rank(fts, to_tsquery('simple', :q)) AS rank,
                               ts_headline('simple', text_body, to_tsquery('simple', :q),
                                           'HighlightAll=true, StartSel=<mark>, StopSel=</mark>') AS snippet
                        FROM mail.emails
                        WHERE fts @@ to_tsquery('simple', :q)
                        ORDER BY rank DESC, sent_at_utc DESC NULLS LAST
                        LIMIT :limit OFFSET :offset";
            } else {
                $sql = "SELECT id, subject, from_addr, to_addrs, sent_at_utc, message_id,
                               ts_rank(fts, to_tsquery('simple', :q)) AS rank
                        FROM mail.emails
                        WHERE fts @@ to_tsquery('simple', :q)
                        ORDER BY rank DESC, sent_at_utc DESC NULLS LAST
                        LIMIT :limit OFFSET :offset";
            }
            $stmt = $pdo->prepare($sql);
            $stmt->bindValue(':q', $q);
        } else {
            // Count with plainto_tsquery (safe for natural text)
            $stmt = $pdo->prepare("SELECT COUNT(*) AS c
                                   FROM mail.emails
                                   WHERE fts @@ plainto_tsquery('simple', :q)");
            $stmt->bindValue(':q', $q);
            $stmt->execute();
            $total = (int)$stmt->fetch()['c'];

            // Page data with rank (and optional snippet)
            if ($showSnippet) {
                $sql = "SELECT id, subject, from_addr, to_addrs, sent_at_utc, message_id,
                               ts_rank(fts, plainto_tsquery('simple', :q)) AS rank,
                               ts_headline('simple', text_body, plainto_tsquery('simple', :q),
                                           'HighlightAll=true, StartSel=<mark>, StopSel=</mark>') AS snippet
                        FROM mail.emails
                        WHERE fts @@ plainto_tsquery('simple', :q)
                        ORDER BY rank DESC, sent_at_utc DESC NULLS LAST
                        LIMIT :limit OFFSET :offset";
            } else {
                $sql = "SELECT id, subject, from_addr, to_addrs, sent_at_utc, message_id,
                               ts_rank(fts, plainto_tsquery('simple', :q)) AS rank
                        FROM mail.emails
                        WHERE fts @@ plainto_tsquery('simple', :q)
                        ORDER BY rank DESC, sent_at_utc DESC NULLS LAST
                        LIMIT :limit OFFSET :offset";
            }
            $stmt = $pdo->prepare($sql);
            $stmt->bindValue(':q', $q);
        }
    } else {
        // No query: show latest emails
        $stmt = $pdo->query("SELECT COUNT(*) AS c FROM mail.emails");
        $total = (int)$stmt->fetch()['c'];

        $sql = "SELECT id, subject, from_addr, to_addrs, sent_at_utc, message_id
                FROM mail.emails
                ORDER BY sent_at_utc DESC NULLS LAST, id DESC
                LIMIT :limit OFFSET :offset";
        $stmt = $pdo->prepare($sql);
    }

    // Bind common pagination params
    $stmt->bindValue(':limit', $pageSize, PDO::PARAM_INT);
    $stmt->bindValue(':offset', $offset, PDO::PARAM_INT);
    $stmt->execute();
    $rows = $stmt->fetchAll();

    header_html('Mail Viewer');

    // Toolbar + Search form
    echo '<div class="toolbar">';
    echo '<form method="get" action="index.php">';
    echo '<input type="hidden" name="action" value="list">';
    echo '<input type="text" name="q" value="' . h($q) . '" placeholder="Search (FTS): subject, addresses, body...">';
    echo '<label><input type="checkbox" name="snippet" value="1"' . ($showSnippet ? ' checked' : '') . '> show snippet</label>';
    echo '<label><input type="checkbox" name="advanced" value="1"' . ($advanced ? ' checked' : '') . '> advanced (to_tsquery)</label>';
    echo '<button class="btn btn-primary" type="submit">Search</button> ';
    echo '<a class="btn" href="index.php?action=list">Clear</a> ';
    echo ($useFts
        ? '<span class="pill">FTS: ON (' . ($advanced ? 'to_tsquery' : 'plainto_tsquery') . ')</span>'
        : '<span class="pill">FTS: OFF</span>');
    echo '<span class="pill">Total: ' . h((string)$total) . '</span>';
    echo '</form>';
    echo '</div>';

    // Results table
    echo '<table>';
    echo '<tr><th class="nowrap">Date</th><th>Subject</th><th>From</th><th>To</th>' . ($useFts ? '<th class="nowrap">Rank</th>' : '') . '</tr>';
    foreach ($rows as $r) {
        $date = $r['sent_at_utc'] ?? '';
        $rank = $useFts ? (isset($r['rank']) ? sprintf('%.4f', (float)$r['rank']) : '') : '';
        echo '<tr>';
        echo '<td class="nowrap">' . h($date) . '</td>';
        echo '<td><a href="index.php?action=view&id=' . h((string)$r['id']) . '">' . h($r['subject'] ?? '(no subject)') . '</a> ' .
             ($r['message_id'] ? '<span class="pill">MsgID</span>' : '') . '</td>';
        echo '<td>' . h($r['from_addr']) . '</td>';
        echo '<td>' . h($r['to_addrs']) . '</td>';
        if ($useFts) echo '<td class="nowrap"><code>' . h($rank) . '</code></td>';
        echo '</tr>';

        // Optional snippet row
        if ($useFts && $showSnippet && isset($r['snippet']) && is_string($r['snippet']) && $r['snippet'] !== '') {
            echo '<tr><td colspan="' . ($useFts ? '5' : '4') . '">';
            echo '<div class="muted">Snippet:</div>';
            // ts_headline returns marked text, safe to render as HTML here
            echo '<div class="card">' . $r['snippet'] . '</div>';
            echo '</td></tr>';
        }
    }
    echo '</table>';

    // Pagination
    $maxPage = max(1, (int)ceil($total / $pageSize));
    $prevPage = max(1, $page - 1);
    $nextPage = min($maxPage, $page + 1);
    $base = 'index.php?action=list&q=' . urlencode($q) . '&page_size=' . $pageSize
          . '&snippet=' . ($showSnippet ? '1' : '0') . '&advanced=' . ($advanced ? '1' : '0');

    echo '<div class="toolbar">';
    echo '<a class="btn" href="' . $base . '&page=1">First</a>';
    echo '<a class="btn" href="' . $base . '&page=' . $prevPage . '">Prev</a>';
    echo '<span class="pill">Page ' . h((string)$page) . ' / ' . h((string)$maxPage) . '</span>';
    echo '<a class="btn" href="' . $base . '&page=' . $nextPage . '">Next</a>';
    echo '<a class="btn" href="' . $base . '&page=' . $maxPage . '">Last</a>';
    echo '</div>';

    footer_html();
}

/**
 * VIEW: Single email + attachments and CID rewriting for inline images
 */
function action_view(): void {
    $pdo = db();
    $id = (int)param('id', '0');
    if ($id <= 0) { http_response_code(400); header_html('Bad request'); echo '<p>Missing or invalid email id.</p>'; footer_html(); return; }

    $stmt = $pdo->prepare("SELECT * FROM mail.emails WHERE id = :id");
    $stmt->bindValue(':id', $id, PDO::PARAM_INT);
    $stmt->execute();
    $email = $stmt->fetch();
    if (!$email) { http_response_code(404); header_html('Not found'); echo '<p>Email not found.</p>'; footer_html(); return; }

    $stmt = $pdo->prepare("SELECT id, filename, content_type, is_inline, content_id, size_bytes, sha256_hex
                           FROM mail.attachments WHERE email_id = :id ORDER BY id ASC");
    $stmt->bindValue(':id', $id, PDO::PARAM_INT);
    $stmt->execute();
    $attachments = $stmt->fetchAll();

    // HTML body CID rewriting: cid:... -> route
    $htmlBody = $email['html_body'] ?? null;
    if (is_string($htmlBody) && $htmlBody !== '') {
        $cidMap = [];
        foreach ($attachments as $a) {
            if (!empty($a['content_id'])) {
                $cidNorm = normalize_cid((string)$a['content_id']);
                if ($cidNorm) {
                    $cidMap[$cidNorm] = 'index.php?action=cid&email_id=' . $id . '&cid=' . urlencode($cidNorm);
                }
            }
        }
        $htmlBody = preg_replace_callback(
            '/(src|href)\s*=\s*["\']cid:([^"\']+)["\']/i',
            function ($m) use ($cidMap) {
                $cid = normalize_cid($m[2]);
                $url = ($cid && isset($cidMap[$cid])) ? $cidMap[$cid] : 'about:blank';
                return $m[1] . '="' . $url . '"';
            },
            $htmlBody
        );
        $htmlBody = preg_replace_callback(
            '/url\(\s*cid:([^)]+)\s*\)/i',
            function ($m) use ($cidMap) {
                $cid = normalize_cid($m[1]);
                $url = ($cid && isset($cidMap[$cid])) ? $cidMap[$cid] : 'about:blank';
                return 'url(' . $url . ')';
            },
            $htmlBody
        );
    }

    header_html('Email ' . (string)$id);

    echo '<div class="card">';
    echo '<h2>' . h($email['subject']) . '</h2>';
    echo '<div class="muted">Date: ' . h($email['sent_at_utc']) . '</div>';
    echo '<div>From: ' . h($email['from_addr']) . '</div>';
    echo '<div>To: ' . h($email['to_addrs']) . '</div>';
    if (!empty($email['cc_addrs'])) echo '<div>CC: ' . h($email['cc_addrs']) . '</div>';
    if (!empty($email['bcc_addrs'])) echo '<div>BCC: ' . h($email['bcc_addrs']) . '</div>';
    if (!empty($email['reply_to'])) echo '<div>Reply-To: ' . h($email['reply_to']) . '</div>';
    if (!empty($email['message_id'])) echo '<div>Message-ID: <code>' . h($email['message_id']) . '</code></div>';
    if (!empty($email['in_reply_to'])) echo '<div>In-Reply-To: <code>' . h($email['in_reply_to']) . '</code></div>';
    if (!empty($email['references_hdr'])) echo '<div>References: <code>' . h($email['references_hdr']) . '</code></div>';
    echo '</div>';

    echo '<div class="row">';
    echo '<div class="col card"><h3>Text body</h3><pre>' . h($email['text_body']) . '</pre></div>';
    echo '<div class="col card"><h3>HTML body</h3>';
    echo '<form method="post"><input type="hidden" name="toggle_html" value="1"><button class="btn" type="submit">Toggle HTML rendering</button></form>';
    $renderHtml = (postv('toggle_html') === '1') || (param('render_html') === '1');
    if (!$renderHtml) {
        echo '<p class="muted">HTML rendering is off by default to avoid XSS. Click "Toggle HTML rendering" to display raw HTML.</p>';
        echo '<pre>' . h($email['html_body']) . '</pre>';
    } else {
        echo '<div style="border:1px solid #ddd;padding:8px;overflow:auto;max-height:60vh;background:#fff">';
        echo $htmlBody ?: '<em>No HTML content</em>';
        echo '</div>';
    }
    echo '</div></div>';

    echo '<div class="card"><h3>Raw headers</h3><pre>' . h($email['raw_headers']) . '</pre></div>';

    echo '<div class="card"><h3>Attachments</h3>';
    if (count($attachments) === 0) {
        echo '<p><em>No attachments.</em></p>';
    } else {
        echo '<ul class="attachments">';
        foreach ($attachments as $a) {
            $inline = $a['is_inline'] ? ' (inline)' : '';
            $cid = $a['content_id'] ? ' CID=' . h((string)$a['content_id']) : '';
            $size = is_null($a['size_bytes']) ? '' : ' • ' . number_format((float)$a['size_bytes']) . ' bytes';
            $href = 'index.php?action=download_attachment&id=' . h((string)$a['id']);
            echo '<li><a href="' . $href . '">' . h($a['filename'] ?: 'attachment') . '</a> ';
            echo '<span class="muted">' . h($a['content_type']) . $inline . $size . $cid . '</span></li>';
        }
        echo '</ul>';
    }
    echo '</div>';

    echo '<p><a href="index.php?action=list">Back to list</a></p>';
    footer_html();
}

/**
 * DOWNLOAD: Attachment by id
 */
function action_download_attachment(): void {
    $pdo = db();
    $id = (int)param('id', '0');
    if ($id <= 0) { http_response_code(400); echo 'Invalid attachment id'; return; }

    $stmt = $pdo->prepare("SELECT filename, content_type, encode(content, 'base64') AS b64
                           FROM mail.attachments WHERE id = :id");
    $stmt->bindValue(':id', $id, PDO::PARAM_INT);
    $stmt->execute();
    $row = $stmt->fetch();
    if (!$row) { http_response_code(404); echo 'Attachment not found'; return; }

    $filename = $row['filename'] ?: 'attachment';
    $ctype = $row['content_type'] ?: 'application/octet-stream';
    $data = base64_decode((string)$row['b64'], true);
    if ($data === false) { http_response_code(500); echo 'Failed to decode attachment'; return; }

    header('Content-Type: ' . $ctype);
    header('Content-Length: ' . (string)strlen($data));
    header('Content-Disposition: attachment; filename="' . str_replace('"', '', $filename) . '"');
    while (ob_get_level()) { ob_end_flush(); }
    echo $data;
}

/**
 * CID: Serve inline image/resource referenced via cid:
 */
function action_cid(): void {
    $pdo = db();
    $emailId = (int)param('email_id', '0');
    $cid = normalize_cid(param('cid', ''));
    if ($emailId <= 0 || !$cid) { http_response_code(400); echo 'Invalid CID request'; return; }

    $stmt = $pdo->prepare("SELECT content_type, content_id, encode(content, 'base64') AS b64
                           FROM mail.attachments
                           WHERE email_id = :email_id AND content_id IS NOT NULL");
    $stmt->bindValue(':email_id', $emailId, PDO::PARAM_INT);
    $stmt->execute();
    $rows = $stmt->fetchAll();

    $found = null;
    foreach ($rows as $r) {
        $cidNorm = normalize_cid((string)$r['content_id']);
        if ($cidNorm === $cid) { $found = $r; break; }
    }
    if (!$found) { http_response_code(404); echo 'CID resource not found'; return; }

    $ctype = $found['content_type'] ?: 'application/octet-stream';
    $data = base64_decode((string)$found['b64'], true);
    if ($data === false) { http_response_code(500); echo 'Failed to decode CID resource'; return; }

    header('Content-Type: ' . $ctype);
    header('Content-Length: ' . (string)strlen($data));
    header('Cache-Control: private, max-age=3600');
    while (ob_get_level()) { ob_end_flush(); }
    echo $data;
}

// Router
$action = param('action', 'list');
switch ($action) {
    case 'list': action_list(); break;
    case 'view': action_view(); break;
    case 'download_attachment': action_download_attachment(); break;
    case 'cid': action_cid(); break;
    default:
        http_response_code(404);
        header_html('Not found');
        echo '<p>Unknown action.</p>';
        footer_html();
        break;
}
