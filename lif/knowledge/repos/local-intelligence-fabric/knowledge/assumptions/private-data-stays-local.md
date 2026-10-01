---
type: assumption
title: Unlabelled application data must never leave the box
statement: Application data is CONFIDENTIAL unless labelled otherwise, and only PUBLIC state may be sent to external services such as Jev.
status: active
confidence: high
tags: [constraint]
grounds:
  - id: g1
    source: "[[src-platform-config]]"
    passage: "[[src-platform-config^p3]]"
    stance: supports
    basis: reported
  - id: g2
    source: "[[src-jev-doc]]"
    passage: "[[src-jev-doc^p2]]"
    stance: supports
    basis: reported
---
