# Security

sha is built so that your messages, memories and documents stay between your devices and your own computer.
If you find a way around that, please tell us privately first.

**Report a vulnerability:** use GitHub's private vulnerability reporting on this repository
("Security" tab → "Report a vulnerability"). Please include what you found, how to reproduce it, and what an
attacker could do with it. We'll acknowledge it within a few days and keep you updated until it's fixed.

Please don't open public issues for security problems, and don't test against other people's computers or
the hosted gateway beyond your own account.

**Status:** the end-to-end protocol (AHA-PQ v1: hybrid X25519 + ML-KEM-768 pairing bound to the QR secret,
then a Double Ratchet with ML-KEM steps) is built on audited primitives (pyca/cryptography, @noble), but the
protocol code itself hasn't had an external review yet. Treat it as pre-release until it has.
