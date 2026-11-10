# Security / Безопасность

**Status:** early preview, not independently security-audited. Test before production use.

- Store API tokens, database credentials and webhook secrets only in the deployment environment (`.env` excluded from Git).
- Restrict the Telegram support supergroup to trusted staff. Bot roles control sending, **not topic visibility**.
- Protect HTTPS webhooks, the database and local material files. Limit server users and keep backups encrypted.
- Ensure your company has a lawful basis for storing client chats and a deletion/retention policy.
- Telegram and MAX attachments require care with size limits, remote URLs and redirects.
- API integrations and media contracts require end-to-end validation with real test accounts.
- Never open a public issue with credentials, customer records, or an unredacted exploit payload.

For vulnerabilities, use GitHub's private vulnerability reporting if enabled, or contact the repository owner privately. Do not publish a proof of concept exposing real users.
