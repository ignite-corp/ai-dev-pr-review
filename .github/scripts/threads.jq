[
  .[] | select(
    .comments.nodes[0].author.login | IN(
      "gemini-code-assist", "github-actions", "github-actions[bot]", "claude", "codex"
    )
  ) |
  {
    author: .comments.nodes[0].author.login,
    path,
    line,
    status: (if .isResolved then "resolved" else "unresolved" end),
    body: (
      .comments.nodes[0].body
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
