# Repository guidance

This repository is the independent Browser lane. Keep the official API out of its runtime and dependencies. Browser runs are foreground, headed, bounded, and read-only. Never add automatic engagement, stealth, CAPTCHA bypass, private GraphQL/XHR access, credential extraction, or profile copying.

Use fresh temporary synthetic roots and local-only Chromium fixtures. Do not inspect operator profiles. Keep profile paths, actor handles, post bodies, cookies, and post URLs out of logs and test output. Report UI assumptions as UNVERIFIED_LIVE_UI until the owner completes private live UAT.
