[
  .[] | select(
    .isOutdated == false and
    (.comments.nodes[0].author.login | IN(
      "gemini-code-assist", "github-actions", "claude", "codex"
    ))
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
