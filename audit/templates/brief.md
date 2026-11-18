# Engagement brief

- **Engagement:** {engagement_id}
- **Control:** AP-01 (three-way match and segregation of duties for payments >= $10,000)
- **Entity:** {entity}
- **Period:** {quarter}
- **Sampling seed:** {seed}
- **Data access:** AP tables through the broker only (`mcp__workspace__save_extract`, `mcp__broker__*`).

Deliverables, all through `mcp__outbox__propose_action`:
1. One workpaper (`file_workpaper`, target `workpaper:AP-01`).
2. One finding per exception (`raise_finding`, target `finding:<finding_id>`).
3. One notification to the lead auditor (`notify_auditor`, target `auditor:lead`).

Follow CLAUDE.md. Everything you propose is independently re-performed before it is filed.
