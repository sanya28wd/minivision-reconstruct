---
status: accepted
date: 2026-10-12
supersedes: 0002
---

# Real and NDA footage is processed on Vertex AI with zero data retention; production depends on RTA residency

Officers will upload real NDA footage, and it must be processed. The free Gemini API tier forbids sensitive data and allows human review and training (terms verified 2026-10-11), so real footage never goes there.

**Decision:** the demo and pilot use **Vertex AI**, the same Gemini models through the same SDK, with:
- zero data retention (caching off, request logging off, abuse-monitoring exception requested)
- the Google Cloud DPA
- billing on the $300 Free Trial welcome credit, which excludes the AI Studio Gemini API and partner models but not Google's own Gemini on Vertex

**Before relying on the credit,** make one test call and confirm in the Cloud billing report that the credit is consumed. Until Vertex is set up, real footage is blocked.

**Production:** Vertex is production-grade. However, no Gemini model is served from a UAE or Middle East region, so processing happens in Mumbai, the EU or the US.
- If the RTA accepts out-of-country processing under Google's DPA, Vertex stays in production.
- If the RTA requires in-country processing, production moves to a UAE-region provider (Azure UAE North or AWS Bedrock me-central-1) and is re-benchmarked first.

The model layer stays swappable. Questions to put to the RTA in writing are in docs/research/GEMINI_NDA_PROCESSING_OPTIONS.md.

## Amendment 2026-10-16 (owner)

Setting up Vertex (RPT-12) is deferred. Testing and truth drafting use a **paid Gemini API key** on the archived clips only. Real or NDA footage stays blocked. Before any is processed, the owner tells the professor that the paid Gemini API is not a zero-retention setup, and the professor decides. RPT-12, and RPT-51 (which depends on it), are parked until then.
