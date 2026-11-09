---
status: accepted
date: 2026-10-12
---

# Access is by a forwardable share link alone: no sign-in, no name entry

The owner sends one link to the professor, who may forward it to reviewers the owner never learns about. The owner chose minimum friction: **opening the share link is enough to use the tool**. There are no passwords, accounts or identity prompts.

**Trade-off (explained to the owner, who accepted it).** Anyone holding the link can view and upload, including NDA footage, and actions cannot be tied to a named person.

**Safeguards that add no friction:**
- The share link carries a long random secret, so it cannot be guessed.
- The admin can **switch off or rotate** a share link at any time, and can keep several links for different groups.
- Each browser gets an anonymous session ID, recorded with the share link used. Uploads, approvals, overrides and Feedback are attributed to "visitor N via link X", so the audit trail still shows that different visitors acted.
- The admin page lists the sessions, and one session can be blocked.
- The Feedback form has an **optional** name field.
- The **admin area** (share links, sessions, Feedback inbox) is separate. It needs the owner's own admin secret, so a share link never reaches it.
- Standard hardening applies: HTTPS, secure session cookies, request rate limits, no secrets in logs.

Revisit if the RTA or the professor asks for identified users, for example before production use.
