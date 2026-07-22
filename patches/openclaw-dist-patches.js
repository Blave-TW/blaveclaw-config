#!/usr/bin/env node
// Blave dist patches for openclaw. Applied at provisioning time (user_data runs
// this right after cloning blaveclaw-config, before the gateway starts).
//
// Why this exists: openclaw upstream fixes land slowly (see issue #83815), so
// targeted fixes we cannot wait for are maintained here and applied onto the
// installed openclaw dist. Rules:
//   - Idempotent: a file already containing the patch marker is skipped.
//   - Fail-open: any failure leaves the stock files in place (a .bak-blave
//     backup is written before editing and restored if syntax check fails);
//     provisioning must never break because of a patch.
//   - Version-safe: patterns are exact-matched against the installed source.
//     If upstream changed the code (new version), the patch logs "pattern not
//     found" and skips — revisit this file on every openclaw upgrade.
//
// Usage: node openclaw-dist-patches.js <openclaw-package-root>

"use strict";
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const MARKER = "Blave patch";

// #83815: splitTelegramCaption measures the RAW markdown, but HTML rendering
// (esp. tableMode padding on markdown tables) can push the visible caption past
// Telegram's 1024-char limit -> sendPhoto 400 "caption is too long" and the
// whole reply is lost. Fix: demote over-long rendered captions to the existing
// follow-up-text path (photo without caption + text as a separate message).
// Tag-stripped length over-counts HTML entities, so the check is conservative.
// Verified against openclaw 2026.6.11; still unfixed in 2026.7.1 / 2026.7.2-beta.1.
const PATCHES = [
	{
		id: "caption-1024-delivery-replies",
		old: `\t\tconst { caption, followUpText } = splitTelegramCaption(isFirstMedia ? params.reply.text ?? void 0 : void 0);
\t\tconst htmlCaption = caption ? renderTelegramHtmlText(caption, { tableMode: params.tableMode }) : void 0;
\t\tif (followUpText) pendingFollowUpText = followUpText;`,
		new: `\t\tlet { caption, followUpText } = splitTelegramCaption(isFirstMedia ? params.reply.text ?? void 0 : void 0);
\t\tlet htmlCaption = caption ? renderTelegramHtmlText(caption, { tableMode: params.tableMode }) : void 0;
\t\t// Blave patch caption-1024-delivery-replies (openclaw #83815)
\t\tif (htmlCaption && htmlCaption.replace(/<[^>]+>/g, "").length > 1024) {
\t\t\tfollowUpText = followUpText ? caption + "\\n\\n" + followUpText : caption;
\t\t\tcaption = void 0;
\t\t\thtmlCaption = void 0;
\t\t}
\t\tif (followUpText) pendingFollowUpText = followUpText;`,
	},
	{
		id: "caption-1024-send",
		old: `\t\tconst htmlCaption = caption ? renderHtmlText(caption) : void 0;
\t\tconst needsSeparateText = Boolean(followUpText);`,
		new: `\t\tlet htmlCaption = caption ? renderHtmlText(caption) : void 0;
\t\t// Blave patch caption-1024-send (openclaw #83815)
\t\tif (htmlCaption && htmlCaption.replace(/<[^>]+>/g, "").length > 1024) {
\t\t\tfollowUpText = followUpText ? caption + "\\n\\n" + followUpText : caption;
\t\t\tcaption = void 0;
\t\t\thtmlCaption = void 0;
\t\t}
\t\tconst needsSeparateText = Boolean(followUpText);`,
	},
	// #98220 / #101250: createReplySessionInitializationRevision compares the ENTIRE
	// session-store entry (JSON.stringify of all ~35 fields — including background
	// bookkeeping like updatedAt/status/startedAt/endedAt/token usage that gets
	// rewritten on every turn). Any such write between snapshot and commit makes the
	// revision look "stale" even though session identity never changed, so reply-
	// session init throws "reply session initialization conflicted" and the Telegram
	// gateway spool-retries the same update forever — session is effectively dead
	// until the gateway is restarted, with no auto-recovery and no user-visible error
	// (message just goes unanswered). Reproduces on plain conversation, not just
	// slash commands or rapid-fire bursts; upstream reports show it can trigger
	// after as little as one turn.
	// Fix: narrow the revision to sessionId + sessionFile only (the only fields that
	// actually identify session identity), backported verbatim from the equivalent
	// logic already shipped in openclaw 2026.7.1 (upstream PR #96847 + the 7.1
	// session-accessor). We are NOT upgrading to 7.1 (2026-07-16 eval: skip, 60+
	// crash-loop regressions upgrading long-lived 6.11 installs), so this stays a
	// standalone backport until we move to a stable release that already has it.
	// Verified against openclaw 2026.6.11 on a throwaway Lightsail test instance
	// (uid 29026, 2026-07-22): pre-patch reproduced a 60+ minute wedge from normal
	// chat (no slash command involved); post-patch, 0 conflicts across a 10-message
	// rapid-fire burst that reliably wedged the pre-patch build within one exchange.
	{
		id: "session-init-revision-narrow",
		old: `function createReplySessionInitializationRevision(entry) {
\treturn JSON.stringify(entry ?? null);
}`,
		new: `function createReplySessionInitializationRevision(entry) {
\t// Blave patch session-init-revision-narrow (openclaw #98220, fixed upstream in 7.1)
\tif (!entry) return JSON.stringify(null);
\tconst revisionEntry = { sessionId: entry.sessionId };
\tif (entry.sessionFile !== void 0) revisionEntry.sessionFile = entry.sessionFile;
\treturn JSON.stringify(revisionEntry);
}`,
	},
];

function log(msg) {
	console.log(`[openclaw-dist-patches] ${msg}`);
}

function syntaxOk(file) {
	return spawnSync(process.execPath, ["--check", file], { stdio: "ignore" }).status === 0;
}

function main() {
	const root = process.argv[2];
	if (!root || !fs.existsSync(path.join(root, "dist"))) {
		log(`ERROR: openclaw root not found: ${root}`);
		process.exit(1);
	}
	const distDir = path.join(root, "dist");
	const files = fs.readdirSync(distDir).filter((f) => f.endsWith(".js"));
	let failed = 0;
	for (const patch of PATCHES) {
		const markerTag = `${MARKER} ${patch.id}`;
		let applied = 0;
		let alreadyPatched = 0;
		for (const name of files) {
			const file = path.join(distDir, name);
			let src;
			try {
				src = fs.readFileSync(file, "utf8");
			} catch {
				continue;
			}
			if (src.includes(markerTag)) {
				alreadyPatched++;
				continue;
			}
			const count = src.split(patch.old).length - 1;
			if (count === 0) continue;
			if (count > 1) {
				log(`SKIP ${patch.id} in ${name}: pattern matched ${count} times, expected 1`);
				failed++;
				continue;
			}
			const backup = `${file}.bak-blave`;
			if (!fs.existsSync(backup)) fs.copyFileSync(file, backup);
			fs.writeFileSync(file, src.replace(patch.old, patch.new));
			if (!syntaxOk(file)) {
				fs.copyFileSync(backup, file);
				log(`FAIL ${patch.id} in ${name}: syntax check failed, restored stock file`);
				failed++;
				continue;
			}
			log(`OK ${patch.id} applied to ${name}`);
			applied++;
		}
		if (applied === 0 && alreadyPatched === 0) {
			log(`WARN ${patch.id}: pattern not found in any dist file (upstream changed? openclaw upgrade needs a patch review)`);
			failed++;
		}
		if (alreadyPatched > 0) log(`OK ${patch.id}: already patched (${alreadyPatched} file[s])`);
	}
	process.exit(failed > 0 ? 1 : 0);
}

main();
