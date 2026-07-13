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
