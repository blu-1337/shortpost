<?php
// ShortPost TikTok token relay. Stateless: it adds the app's client secret to a token request and forwards it to TikTok.
// It stores nothing, writes no logs and keeps no copy of any token. See https://shortpost.medianopol.com/privacy-policy.html
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

function out($code, $arr) { http_response_code($code); echo json_encode($arr); exit; }

$cfg = @include __DIR__ . '/../relay-private/config.php';
if (!is_array($cfg) || empty($cfg['client_key']) || empty($cfg['client_secret'])) {
    out(500, ['error' => 'relay_not_configured']);
}

if ($_SERVER['REQUEST_METHOD'] === 'GET') {          // public client key, needed to build the TikTok login URL
    out(200, ['client_key' => $cfg['client_key']]);
}
if ($_SERVER['REQUEST_METHOD'] !== 'POST') out(405, ['error' => 'method_not_allowed']);

$in = json_decode(file_get_contents('php://input', false, null, 0, 20000), true);
if (!is_array($in)) out(400, ['error' => 'bad_request']);

$allowed_redirects = ['http://localhost:8766/callback/'];
$fields = ['client_key' => $cfg['client_key'], 'client_secret' => $cfg['client_secret']];
$grant = $in['grant_type'] ?? '';
if ($grant === 'authorization_code') {
    $code = (string)($in['code'] ?? ''); $ver = (string)($in['code_verifier'] ?? ''); $red = (string)($in['redirect_uri'] ?? '');
    if ($code === '' || $ver === '' || !in_array($red, $allowed_redirects, true)) out(400, ['error' => 'bad_request']);
    $fields += ['code' => $code, 'grant_type' => 'authorization_code', 'redirect_uri' => $red, 'code_verifier' => $ver];
} elseif ($grant === 'refresh_token') {
    $rt = (string)($in['refresh_token'] ?? '');
    if ($rt === '') out(400, ['error' => 'bad_request']);
    $fields += ['grant_type' => 'refresh_token', 'refresh_token' => $rt];
} else {
    out(400, ['error' => 'bad_request']);
}

$ch = curl_init('https://open.tiktokapis.com/v2/oauth/token/');
curl_setopt_array($ch, [
    CURLOPT_POST => true, CURLOPT_POSTFIELDS => http_build_query($fields),
    CURLOPT_HTTPHEADER => ['Content-Type: application/x-www-form-urlencoded'],
    CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 20,
]);
$body = curl_exec($ch);
$status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);
if ($body === false) out(502, ['error' => 'upstream_unreachable']);
http_response_code($status ?: 502);
echo $body;
