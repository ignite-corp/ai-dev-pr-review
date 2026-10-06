# Every GitHub spelling of an app login reduced to the bare slug, as
# normalize_bot_login in github_pr_support.py does: GraphQL `x`, REST
# `x[bot]`, `gh` display `app/x`.
def norm: sub("^app/"; "") | sub("\\[bot\\]$"; "");
# Match the same first-line envelope as inline_post_envelope in
# github_pr_support.py before applying the context limit or dedup key.
def strip_inline_envelope:
  sub("^<!-- inline-post-(claude|codex|gemini)-r[1-9][0-9]* run:(actions-[1-9][0-9]*-[1-9][0-9]*|local-[0-9a-f]{32}) -->\\r?\\n"; "");
# `bot` is BOT_LOGIN, the identity the inline comments were posted under --
# the reviewer App on a cross run (AT-2520). Optional, so a caller that
# passes no --arg keeps the fixed list.
(["gemini-code-assist", "github-actions", "claude", "codex"]
  + ([$ARGS.named.bot // "" | norm] | map(select(. != "")))) as $trusted
| [
  .[] | select(
    .comments.nodes[0].author.login // "" | norm | IN($trusted[])
  ) |
  {
    author: .comments.nodes[0].author.login,
    path,
    line,
    status: (if .isResolved then "resolved" else "unresolved" end),
    body: (
      .comments.nodes[0].body
      | strip_inline_envelope
      | if length > 500 then .[:500] + "...(truncated)" else . end
    )
  }
]
# Outdated threads are intentionally kept: after a push most prior threads
# become outdated, and dropping them hides exactly the findings the reviewer
# is about to re-raise. Unresolved threads sort first so the downstream
# 50-item prompt cap never crowds them out.
| sort_by(.status != "unresolved")
# Exact-match dedup on (path, normalized body): the same finding repeated
# across review rounds otherwise consumes prompt-cap slots that could carry
# distinct findings. Unresolved entries sort first, so keeping the first
# occurrence preserves unresolved precedence when duplicates collide.
| reduce .[] as $t ({seen: {}, out: []};
    ((($t.path // "") + "\u001f"
      + (($t.body // "") | ascii_downcase | [scan("\\w+")] | join(" "))))
    as $key
    | if .seen[$key] then .
      else {seen: (.seen + {($key): true}), out: (.out + [$t])}
      end
  )
| .out
