[
  .[] | select(
    .isResolved == false and
    .isOutdated == false and
    (.comments.nodes[0].author.login | IN("gemini-code-assist", "github-actions"))
  ) |
  {
    author: .comments.nodes[0].author.login,
    path,
    line,
    body: (
      .comments.nodes[0].body
      | if length > 500 then .[:500] + "...(truncated)" else . end
    )
  }
]
