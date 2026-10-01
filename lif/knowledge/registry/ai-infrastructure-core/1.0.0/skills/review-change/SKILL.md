---
type: skill
name: review-change
version: 1
title: Review a production change against prior knowledge
description: Before changing infrastructure, gather the decisions, incidents, methods and assumptions that bear on it.
when: Any change to a deployment, service, model alias, scheduler policy or manifest that reaches production.
inputs: [target]
outputs: [plan that cites the decisions it follows, change object after execution]
method: "[[knowledge-aware-change]]"
status: active
---
1. `assemble_context` with the change description (profile `engineering`).
2. `why_decision` for every accepted decision in the package that the change touches.
3. `change_impact` on the target: list affected decisions, tasks and deployments.
4. If any assumption in the chain is challenged/invalidated, stop and raise a reconsideration review first.
5. Execute; then create a `change` object (`subject`, `reason`, `because:` the decision) and checkpoint.
