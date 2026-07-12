# Password reset — SSO / directory account

1. Go to `https://password.firm.internal` from any browser (works off-VPN).
2. Enter your username and choose "Forgot / expired password".
3. Verify with MFA (push or hardware key). No MFA enrolled? Escalate — a human must verify identity before a reset.
4. Set the new password: minimum 14 characters, not one of your last 10.
5. Lock and unlock your laptop once so the local cache picks up the new password, then re-sign into Slack/email when prompted.
6. If the portal says "account locked by administrator": do not retry — escalate immediately (possible security hold).
