---
pv_schema: 1
id: ledger_<project-slug>
memory_type: canonical
kind: decision_ledger
review_state: human_accepted
temporal_state: current
outcome: not_applicable
projects: ["<Project>"]
provenance_mode: human_asserted
relations: {"supports":[],"contradicts":[],"supersedes":[]}
retrieval_tier: primary
privacy: normal
---

# <Project> 결정 장부

Keep row IDs and append new rows. Only state and follow-up change, except an exact reviewed ownership transfer under CURATOR.md.
Types: 결정, 미룸, 대체, 목표변경, 질문. States: 유효, 대체됨, 보류, 종결; 이관됨 is a non-authoritative ownership redirect.

| id | 날짜 | 유형 | 상태 | 내용 | 이유·근거 | 후속 |
| --- | --- | --- | --- | --- | --- | --- |
