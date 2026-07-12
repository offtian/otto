# VPN — connect or fix a broken connection

1. Open the GlobalConnect client. If it is not installed, install it from the Self Service portal (search "VPN").
2. Enter the gateway address `vpn.firm.internal` and sign in with your SSO credentials. Approve the MFA push on your phone.
3. If the client says "connection timed out": switch networks (hotspot vs office Wi-Fi) to rule out the local network, then retry.
4. If sign-in fails with "account locked": your password has expired — follow the password-reset runbook first, then retry the VPN.
5. Still failing: collect the client logs (Help → Export logs) and escalate with the error message and the time it happened.
