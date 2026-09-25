<?php
/**
 * BizFluxLab Approval Relay -- PURE core.
 *
 * No WordPress dependency, no ABSPATH guard, no superglobals, no I/O.
 * Directly executable under `php` CLI (see tests/run.php). The MU-plugin
 * `bizfluxlab-approval-relay.php` require_once's this file.
 *
 * AUTHORITY
 * ---------------------------------------------------------------------------
 * This relay is NOT the content authority. affiliate-ai (local) owns the change
 * request, the proposal hash/version, staleness, the approval record and every
 * WordPress mutation. The relay only:
 *   - shows a sanitized, immutable snapshot of one proposal
 *   - verifies a one-time capability
 *   - records exactly one explicit human decision
 *   - hands that decision to authenticated affiliate-ai polling
 * A relay decision can never edit an article: this code has no article writer.
 *
 * SCANNER SAFETY (hard requirement)
 * ---------------------------------------------------------------------------
 * A GET can never approve, reject or consume a capability. The review page GET
 * reads NOTHING from the database: the capability lives in the URL fragment,
 * which browsers do not send. The page's JS exchanges it over a POST. Mail
 * scanners and preview bots therefore cannot reach any state transition.
 *
 * Contains:
 *   BFL_Approval_Capability -- digest + binding verification (mirrors
 *                              app/approval/capability.py byte for byte)
 *   BFL_Approval_State      -- the decision state machine
 *   BFL_Approval_Render     -- escaping + mobile review HTML
 *   bfl_approval_headers()  -- noindex / no-store / CSP header set
 *
 * PHP requirement: 8.0+
 */

declare( strict_types = 1 );

/* =========================================================================
 * Capability verification.
 *
 * Python reference (app/approval/capability.py):
 *   digest  = sha256(secret)
 *   binding = sha256( implode(chr(31), [type, id, hash, version, expiry_iso]) )
 * The relay stores ONLY digest + binding. The raw capability is never stored,
 * never logged and never returned.
 * ========================================================================= */
final class BFL_Approval_Capability {

	/** Same vocabulary as the Python side; nothing more is ever revealed. */
	public const REASON_OK          = 'ok';
	public const REASON_MALFORMED   = 'malformed_capability';
	public const REASON_MISMATCH    = 'capability_mismatch';
	public const REASON_EXPIRED     = 'expired';
	public const REASON_BINDING     = 'binding_mismatch';

	public static function is_well_formed( $secret ) : bool {
		return is_string( $secret ) && 1 === preg_match( '/\A[A-Za-z0-9_-]{43,86}\z/', $secret );
	}

	public static function digest( string $secret ) : string {
		return hash( 'sha256', $secret );
	}

	public static function binding(
		string $subject_type,
		int $subject_id,
		string $subject_hash,
		int $subject_version,
		string $expires_at_iso
	) : string {
		$payload = implode(
			chr( 31 ),
			array(
				$subject_type,
				(string) $subject_id,
				$subject_hash,
				(string) $subject_version,
				$expires_at_iso,
			)
		);
		return hash( 'sha256', $payload );
	}

	/**
	 * @return array{0:bool,1:string} [ok, reason]
	 */
	public static function verify(
		string $presented,
		array $session,
		int $now_unix
	) : array {
		if ( ! self::is_well_formed( $presented ) ) {
			return array( false, self::REASON_MALFORMED );
		}
		if ( ! hash_equals( (string) ( $session['capability_digest'] ?? '' ), self::digest( $presented ) ) ) {
			return array( false, self::REASON_MISMATCH );
		}
		$expires = (int) ( $session['expires_at_unix'] ?? 0 );
		if ( $now_unix >= $expires ) {
			return array( false, self::REASON_EXPIRED );
		}
		return array( true, self::REASON_OK );
	}
}

/* =========================================================================
 * Decision state machine.
 *
 * Mirrors the local envelope (app/models/mobile_approval.py) but only for the
 * states the relay itself can be in. The relay never invents a decision.
 * ========================================================================= */
final class BFL_Approval_State {

	public const PENDING   = 'pending';
	public const DECIDED   = 'decided';
	public const CONSUMED  = 'consumed';
	public const EXPIRED   = 'expired';
	public const REVOKED   = 'revoked';

	/**
	 * Can this session still accept a human decision?
	 *
	 * @return array{0:bool,1:string} [ok, reason]
	 */
	public static function can_decide( array $session, int $now_unix ) : array {
		$state = (string) ( $session['state'] ?? '' );
		if ( self::DECIDED === $state || self::CONSUMED === $state ) {
			return array( false, 'already_decided' );
		}
		if ( self::REVOKED === $state ) {
			return array( false, 'session_not_pending' );
		}
		if ( self::PENDING !== $state ) {
			return array( false, 'session_not_pending' );
		}
		if ( $now_unix >= (int) ( $session['expires_at_unix'] ?? 0 ) ) {
			return array( false, 'session_expired' );
		}
		return array( true, 'ok' );
	}

	/** Can this session still be displayed (read-only)? */
	public static function can_review( array $session, int $now_unix ) : array {
		$state = (string) ( $session['state'] ?? '' );
		if ( self::REVOKED === $state ) {
			return array( false, 'session_not_pending' );
		}
		if ( $now_unix >= (int) ( $session['expires_at_unix'] ?? 0 ) ) {
			return array( false, 'session_expired' );
		}
		return array( true, 'ok' );
	}

	public static function is_valid_decision( $decision ) : bool {
		return 'approved' === $decision || 'rejected' === $decision;
	}
}

/* =========================================================================
 * Rendering. Every dynamic value is escaped. Proposal text is NEVER treated
 * as trusted HTML -- it is escaped and placed inside <pre>/<td> as text.
 * ========================================================================= */
final class BFL_Approval_Render {

	public static function esc( $value ) : string {
		return htmlspecialchars( (string) ( $value ?? '' ), ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8' );
	}

	/**
	 * Pure snapshot -> display model (T6.1). No DOM, no network, so it is
	 * executed directly by the test harness under Node.
	 *
	 * The text shown is the exact text the human is deciding on:
	 * threads_post -> publish_text, change_request -> inserted_paragraph.
	 * Missing or blank text fails closed: canApprove is false and the page
	 * shows an error instead of an approve button.
	 */
	public static function review_model_js() : string {
		return <<<'JS'
function reviewModel(s){
 s=s||{};
 var threads=s.subject_type==="threads_post";
 var raw=threads?s.publish_text:s.inserted_paragraph;
 var ok=typeof raw==="string"&&raw.replace(/\s+/g,"")!=="";
 var rows=threads?[
  ["投稿案","#"+s.subject_id],["記事",s.article_title],["切り口",s.angle],
  ["リンク",s.link_mode],["文字数",s.character_count],["提案",s.subject_hash_short],
  ["期限",s.expires_at_local]
 ]:[
  ["変更要求","#"+s.subject_id],["記事",s.article_title],["リンク先",s.target_article_title],
  ["変更種別",s.change_type],["候補",s.candidate_type],["優先度",s.priority],
  ["提案","v"+s.subject_version+" "+s.subject_hash_short],["アンカー",s.anchor_text],
  ["期限",s.expires_at_local]
 ];
 return {
  threads:threads,
  rows:rows,
  textLabel:threads?"投稿される本文":"挿入される段落",
  text:ok?raw:"",
  canApprove:ok,
  error:ok?null:(threads?"投稿される本文を表示できません。この画面からは承認できません。":"挿入される段落を表示できません。この画面からは承認できません。"),
  approveQuestion:threads?"この投稿案を承認しますか。承認後、PC 側の同期処理が安全性を再確認したうえで記録します。":"この変更を承認しますか。承認後、PC 側の同期処理が安全性を再確認したうえで記録します。",
  rationale:threads?null:(s.rationale==null?"":String(s.rationale)),
  contextBefore:threads?null:(s.context_before||null),
  contextAfter:threads?null:(s.context_after||null),
  diffLines:threads?[]:(s.diff_lines||[]),
  diffTruncated:!threads&&!!s.diff_truncated,
  warnings:(s.warnings||[]).map(function(w){return String(w);})
 };
}
JS;
	}

	/**
	 * The review page shell. Contains NO proposal data: the capability lives in
	 * the URL fragment and the snapshot is fetched by an explicit POST. A mail
	 * scanner that fetches this URL therefore learns nothing and changes nothing.
	 */
	public static function shell(
		string $relay_session_id,
		string $exchange_route,
		string $decision_route,
		string $script_nonce
	) : string {
		$sid   = self::esc( $relay_session_id );
		$ex    = self::esc( $exchange_route );
		$dec   = self::esc( $decision_route );
		$nonce = self::esc( $script_nonce );
		$model = self::review_model_js();
		return <<<HTML
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive">
<title>BizFluxLab 承認</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;padding:16px;line-height:1.7;color:#1b1b1b;background:#fff}
 main{max-width:640px;margin:0 auto}
 h1{font-size:18px;margin:0 0 12px}
 dl{display:grid;grid-template-columns:8em 1fr;gap:4px 12px;font-size:14px;margin:0 0 16px}
 dt{color:#555}
 dd{margin:0;word-break:break-word}
 pre{background:#f4f4f4;padding:12px;border-radius:6px;overflow-x:auto;font-size:12px;white-space:pre-wrap;word-break:break-word}
 .warn{background:#fff4e5;border-left:4px solid #d98324;padding:8px 12px;font-size:13px}
 button{font-size:16px;padding:14px 20px;border-radius:8px;border:0;width:100%;margin-top:10px}
 .approve{background:#1a4d8f;color:#fff}
 .reject{background:#fff;color:#8f1a1a;border:1px solid #8f1a1a}
 .msg{padding:12px;border-radius:6px;font-size:14px}
 .err{background:#fdeaea}
 .ok{background:#eaf7ee}
 textarea{width:100%;font-size:15px;padding:8px;box-sizing:border-box}
</style>
</head>
<body>
<main>
<h1>BizFluxLab 承認</h1>
<div id="app"><p class="msg">読み込み中...</p></div>
</main>
<script nonce="{$nonce}">
(function(){
 var SID="{$sid}";
 var EXCHANGE="{$ex}";
 var DECIDE="{$dec}";
 var app=document.getElementById("app");
 var cap=(location.hash||"").replace(/^#/,"");
 var nonce=null;
 function esc(s){var d=document.createElement("div");d.appendChild(document.createTextNode(s==null?"":String(s)));return d.innerHTML;}
 function msg(text,cls){app.innerHTML='<p class="msg '+cls+'">'+esc(text)+"</p>";}
 if(!cap){msg("このリンクは無効です。","err");return;}
 /* Remove the capability from the address bar as soon as it is read. */
 history.replaceState(null,"",location.pathname);
 fetch(EXCHANGE,{method:"POST",headers:{"Content-Type":"application/json"},
   credentials:"same-origin",body:JSON.stringify({relay_session_id:SID,capability:cap})})
  .then(function(r){return r.json().then(function(j){return {ok:r.ok,body:j};});})
  .then(function(res){
    if(!res.ok){msg("このリンクは使用できません。","err");return;}
    nonce=res.body.nonce;render(res.body.snapshot);
  }).catch(function(){msg("通信に失敗しました。","err");});
 function row(k,v){return v==null||v===""?"":"<dt>"+esc(k)+"</dt><dd>"+esc(v)+"</dd>";}
 {$model}
 function render(s){
  var m=reviewModel(s);
  var h="<dl>";
  m.rows.forEach(function(r){h+=row(r[0],r[1]);});
  h+="</dl>";
  if(!m.threads){h+="<p>"+esc(m.rationale)+"</p>";}
  if(m.contextBefore){h+="<p><strong>直前</strong></p><pre>"+esc(m.contextBefore)+"</pre>";}
  h+="<p><strong>"+esc(m.textLabel)+"</strong></p>";
  /* Fail closed: without the exact text there is nothing to approve. */
  h+=m.canApprove?'<pre id="t">'+esc(m.text)+"</pre>":'<p class="msg err" id="t">'+esc(m.error)+"</p>";
  if(m.contextAfter){h+="<p><strong>直後</strong></p><pre>"+esc(m.contextAfter)+"</pre>";}
  if(m.diffLines.length){h+="<p><strong>差分</strong></p><pre>"+esc(m.diffLines.join("\\n"))+(m.diffTruncated?"\\n...":"")+"</pre>";}
  m.warnings.forEach(function(w){h+='<p class="warn">'+esc(w)+"</p>";});
  if(m.canApprove){h+='<button class="approve" id="a">承認する</button>';}
  h+='<button class="reject" id="r">却下する</button>';
  app.innerHTML=h;
  if(m.canApprove){document.getElementById("a").onclick=function(){confirmApprove(m);};}
  document.getElementById("r").onclick=function(){confirmReject();};
 }
 function confirmApprove(m){
  if(!m||!m.canApprove){return;}
  app.innerHTML="<p>"+esc(m.approveQuestion)+"</p>"+
    "<pre>"+esc(m.text)+"</pre>"+
    '<button class="approve" id="y">承認を確定する</button><button class="reject" id="n">やめる</button>';
  document.getElementById("y").onclick=function(){send("approved",null);};
  document.getElementById("n").onclick=function(){location.reload();};
 }
 function confirmReject(){
  app.innerHTML="<p>却下の理由 (任意)</p><textarea id=\\"why\\" rows=\\"3\\"></textarea>"+
    '<button class="reject" id="y">却下を確定する</button><button class="approve" id="n">やめる</button>';
  document.getElementById("y").onclick=function(){send("rejected",document.getElementById("why").value);};
  document.getElementById("n").onclick=function(){location.reload();};
 }
 function send(decision,reason){
  app.innerHTML='<p class="msg">送信中...</p>';
  fetch(DECIDE,{method:"POST",headers:{"Content-Type":"application/json","X-BFL-Approval-Nonce":nonce},
    credentials:"same-origin",body:JSON.stringify({relay_session_id:SID,decision:decision,reason:reason,nonce:nonce})})
   .then(function(r){return r.json().then(function(j){return {ok:r.ok,body:j};});})
   .then(function(res){
     if(!res.ok){msg("記録できませんでした。","err");return;}
     msg(decision==="approved"?"承認を受け付けました。":"却下を受け付けました。","ok");
   }).catch(function(){msg("通信に失敗しました。","err");});
 }
})();
</script>
</body>
</html>
HTML;
	}

	/** Minimal page for an unusable link. Reveals nothing about the subject. */
	public static function unavailable() : string {
		return "<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
			. "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
			. "<meta name=\"robots\" content=\"noindex, nofollow, noarchive\">"
			. "<title>BizFluxLab</title></head><body style=\"font-family:system-ui,sans-serif;padding:24px\">"
			. "<p>このリンクは使用できません。</p></body></html>";
	}
}

/**
 * Headers applied to every relay response. The approval page must never become
 * public content, must never be cached, and must never leak its URL onward.
 *
 * @return array<string,string>
 */
function bfl_approval_headers( string $script_nonce = '' ) : array {
	// The page carries exactly one inline script. Bind it with a per-response
	// nonce instead of allowing arbitrary inline script.
	$script_src = '' !== $script_nonce ? "'nonce-{$script_nonce}'" : "'none'";
	return array(
		'X-Robots-Tag'            => 'noindex, nofollow, noarchive',
		'Cache-Control'           => 'no-store, no-cache, must-revalidate, private',
		'Pragma'                  => 'no-cache',
		'Referrer-Policy'         => 'no-referrer',
		'X-Frame-Options'         => 'DENY',
		'X-Content-Type-Options'  => 'nosniff',
		'Content-Security-Policy' => "default-src 'none'; script-src {$script_src}; "
			. "style-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; "
			. "base-uri 'none'; frame-ancestors 'none'; img-src 'none'",
	);
}

/**
 * Build the sanitized payload the review page receives. The relay stores the
 * snapshot affiliate-ai gave it; this only adds display-time fields and drops
 * anything that should never reach a browser.
 *
 * @return array<string,mixed>
 */
function bfl_approval_public_snapshot( array $snapshot, string $expires_at_local ) : array {
	$allowed = array(
		'subject_type', 'subject_id', 'subject_version', 'subject_hash_short',
		'change_type', 'article_id', 'article_title', 'target_article_id',
		'target_article_title', 'candidate_type', 'priority', 'rationale',
		'anchor_text', 'inserted_paragraph', 'context_before', 'context_after',
		'insertion_line', 'diff_lines', 'diff_truncated', 'warnings',
		// threads_post (T6.1): the exact text to be published and its context.
		'angle', 'link_mode', 'publish_text', 'character_count',
	);
	$out = array();
	foreach ( $allowed as $key ) {
		if ( array_key_exists( $key, $snapshot ) ) {
			$out[ $key ] = $snapshot[ $key ];
		}
	}
	$out['expires_at_local'] = $expires_at_local;
	return $out;
}

/**
 * The exact text the human decides on, or null when there is none (T6.1).
 *
 * threads_post -> publish_text, change_request -> inserted_paragraph.
 */
function bfl_approval_review_text( array $snapshot ) : ?string {
	$type = (string) ( $snapshot['subject_type'] ?? '' );
	$key  = 'threads_post' === $type ? 'publish_text' : ( 'change_request' === $type ? 'inserted_paragraph' : '' );
	if ( '' === $key ) {
		return null;
	}
	$text = $snapshot[ $key ] ?? null;
	if ( ! is_string( $text ) || '' === trim( $text ) ) {
		return null;
	}
	return $text;
}

/**
 * Server-side fail-closed rule (T6.1): an approval is only accepted when the
 * stored snapshot carried reviewable text. A rejection is always allowed.
 *
 * @return array{0:bool,1:string,2:int} [ok, reason, http_status]
 */
function bfl_approval_decision_content_guard( array $snapshot, string $decision ) : array {
	if ( 'approved' === $decision && null === bfl_approval_review_text( $snapshot ) ) {
		return array( false, 'review_text_missing', 409 );
	}
	return array( true, 'ok', 200 );
}

/* =========================================================================
 * Browser cookie seam (C8.8.2).
 *
 * The first production rejection failed here: the confirmation cookie was
 * scoped to the review page prefix (/bfl-approval/) while the decision POST
 * goes to the REST namespace (/wp-json/affiliate-ai/v1/...). Under RFC 6265
 * those are disjoint path trees, so the browser never sent the cookie and the
 * decision was refused every time.
 *
 * The rule below is the actual browser behaviour, so a test can assert that
 * the cookie we set really would be delivered to the route the page posts to.
 * ========================================================================= */

/**
 * RFC 6265 section 5.1.4 path-match. Returns true when a browser would send a
 * cookie scoped to $cookie_path on a request for $request_path.
 */
function bfl_approval_path_matches( string $request_path, string $cookie_path ) : bool {
	if ( '' === $cookie_path ) {
		return false;
	}
	if ( $request_path === $cookie_path ) {
		return true;
	}
	if ( 0 !== strpos( $request_path, $cookie_path ) ) {
		return false;
	}
	if ( '/' === substr( $cookie_path, -1 ) ) {
		return true;
	}
	return '/' === substr( $request_path, strlen( $cookie_path ), 1 );
}

/**
 * The browser-facing review routes, derived from ONE base so the cookie scope
 * and the routes the page posts to cannot drift apart again.
 *
 * @return array{exchange:string,decide:string}
 */
function bfl_approval_review_routes( string $rest_base ) : array {
	$base = rtrim( $rest_base, '/' ) . '/';
	return array(
		'exchange' => $base . 'approval-review/exchange',
		'decide'   => $base . 'approval-review/decide',
	);
}

/**
 * The decision guard, extracted so it is testable without WordPress or MySQL.
 *
 * Order matters: possession of the review session is checked before anything
 * about the subject is revealed.
 *
 * @param array  $session        relay row (state, expires_at_unix)
 * @param string $cookie         value the browser sent, '' when absent
 * @param mixed  $stored_digest  sha256 of the nonce issued at exchange
 * @param string $header_nonce   X-BFL-Approval-Nonce
 *
 * @return array{0:bool,1:string,2:int} [ok, reason, http_status]
 */
function bfl_approval_decision_guard(
	array $session,
	string $cookie,
	$stored_digest,
	string $header_nonce,
	int $now_unix
) : array {
	// No cookie means the browser never established (or could not return) the
	// review session. This is exactly what the path bug produced.
	if ( '' === $cookie || ! is_string( $stored_digest ) || '' === $stored_digest ) {
		return array( false, 'session_not_found', 403 );
	}
	if ( ! hash_equals( $stored_digest, hash( 'sha256', $cookie ) ) ) {
		return array( false, 'session_not_found', 403 );
	}
	if ( ! hash_equals( $cookie, $header_nonce ) ) {
		return array( false, 'session_not_found', 403 );
	}
	list( $ok, $reason ) = BFL_Approval_State::can_decide( $session, $now_unix );
	if ( ! $ok ) {
		return array( false, $reason, 409 );
	}
	return array( true, 'ok', 200 );
}
