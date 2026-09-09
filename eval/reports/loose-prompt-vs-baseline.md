# Eval comparison

- Baseline: `baseline` (prompt `v2-strict-tools`)
- Candidate: `loose-prompt` (prompt `v1-loose`)

| Metric | Baseline | Candidate | Δ |
|---|---:|---:|---:|
| task_completion_rate | 1.0 | 0.7609 | ▼ -0.2391 |
| correct_tool_sequence_rate | 1.0 | 0.3913 | ▼ -0.6087 |
| hallucinated_tool_call_rate | 0.0 | 0.0 | — +0.0000 |
| avg_turns_to_resolution | 3.76 | 8.26 | ▲ +4.5000 |
| pass_rate | 1.0 | 0.3913 | ▼ -0.6087 |

**Regression detected: YES**
