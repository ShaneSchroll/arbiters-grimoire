auth.py -----------

Spoof-resistant client IP added.

Two-layer login throttle via a new bounded, thread-safe _RateLimiter: per-(ip, email) (10/15 min) catches single-account hammering, and per-IP (30/15 min) catches password-spraying across rotating accounts.

Registration is now throttled and constant-time.

Enumeration timing leak fixed.

Expired sessions and used/expired reset tokens are deleted on startup - no more unbounded db growth.

------------------------------------------------------------------------------------------------------------------------------------
server.py -----------

Per-user chat rate limit.

Hard input caps on /api/chat: max 20 messages, 8K chars/message, 24K chars total, and a strict role/content schema.

Elimated unvalidated-input 500s.

Security headers (CSP, X-Frame-Options: DENY, nosniff, Referrer-Policy, HSTS) via a pure-ASGI middleware. The CSP is tailored to what we load now.