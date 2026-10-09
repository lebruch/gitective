"""Secret-detection regexes, used by 'secrets'. Sourced from TruffleHog's
classic regex set:
https://raw.githubusercontent.com/dxa4481/truffleHogRegexes/master/truffleHogRegexes/regexes.json
(fetched 2026-09-24, vendored rather than fetched at runtime - no network
dependency, no risk of the upstream file changing/disappearing underneath
this tool).

That JSON file has two entries literally named "AWS API Key" - standard
JSON parsing silently keeps only the second, narrower one (AKIA-prefixed
only) and drops the first (which also covers AGPA/AIDA/AROA/... prefixes,
other IAM entity types). Both are kept here under distinct names instead.

Intentionally broad, not exhaustive - expect false positives, especially
from "Generic API Key"/"Generic Secret" (they match on the surrounding
variable name, not a specific key format). Results should be eyeballed,
not treated as confirmed leaks.
"""

import re

_PATTERN_SOURCES = {
    "Slack Token": r"(xox[pborsa]-[0-9]{12}-[0-9]{12}-[0-9]{12}-[a-z0-9]{32})",
    "RSA private key": r"-----BEGIN RSA PRIVATE KEY-----",
    "SSH (DSA) private key": r"-----BEGIN DSA PRIVATE KEY-----",
    "SSH (EC) private key": r"-----BEGIN EC PRIVATE KEY-----",
    "PGP private key block": r"-----BEGIN PGP PRIVATE KEY BLOCK-----",
    "AWS API Key (broad)": r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}",
    "AWS API Key": r"AKIA[0-9A-Z]{16}",
    "Amazon MWS Auth Token": r"amzn\.mws\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    "AWS AppSync GraphQL Key": r"da2-[a-z0-9]{26}",
    "Facebook Access Token": r"EAACEdEose0cBA[0-9A-Za-z]+",
    "Facebook OAuth": r"[fF][aA][cC][eE][bB][oO][oO][kK].*['\"][0-9a-f]{32}['\"]",
    "GitHub": r"[gG][iI][tT][hH][uU][bB].*['\"][0-9a-zA-Z]{35,40}['\"]",
    "Generic API Key": r"[aA][pP][iI]_?[kK][eE][yY].*['\"][0-9a-zA-Z]{32,45}['\"]",
    "Generic Secret": r"[sS][eE][cC][rR][eE][tT].*['\"][0-9a-zA-Z]{32,45}['\"]",
    "Google API Key": r"AIza[0-9A-Za-z\-_]{35}",
    "Google Cloud Platform OAuth": r"[0-9]+-[0-9A-Za-z_]{32}\.apps\.googleusercontent\.com",
    "Google (GCP) Service-account": r"\"type\":\s*\"service_account\"",
    "Google OAuth Access Token": r"ya29\.[0-9A-Za-z\-_]+",
    "Heroku API Key": r"[hH][eE][rR][oO][kK][uU].*[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}",
    "MailChimp API Key": r"[0-9a-f]{32}-us[0-9]{1,2}",
    "Mailgun API Key": r"key-[0-9a-zA-Z]{32}",
    "Password in URL": r"[a-zA-Z]{3,10}://[^/\s:@]{3,20}:[^/\s:@]{3,20}@.{1,100}[\"'\s]",
    "PayPal Braintree Access Token": r"access_token\$production\$[0-9a-z]{16}\$[0-9a-f]{32}",
    "Picatic API Key": r"sk_live_[0-9a-z]{32}",
    "Slack Webhook": r"https://hooks\.slack\.com/services/T[a-zA-Z0-9_]{8}/B[a-zA-Z0-9_]{8}/[a-zA-Z0-9_]{24}",
    "Stripe API Key": r"sk_live_[0-9a-zA-Z]{24}",
    "Stripe Restricted API Key": r"rk_live_[0-9a-zA-Z]{24}",
    "Square Access Token": r"sq0atp-[0-9A-Za-z\-_]{22}",
    "Square OAuth Secret": r"sq0csp-[0-9A-Za-z\-_]{43}",
    "Telegram Bot API Key": r"[0-9]+:AA[0-9A-Za-z\-_]{33}",
    "Twilio API Key": r"SK[0-9a-fA-F]{32}",
    "Twitter Access Token": r"[tT][wW][iI][tT][tT][eE][rR].*[1-9][0-9]+-[0-9a-zA-Z]{40}",
    "Twitter OAuth": r"[tT][wW][iI][tT][tT][eE][rR].*['\"][0-9a-zA-Z]{35,44}['\"]",
}

PATTERNS = {name: re.compile(source) for name, source in _PATTERN_SOURCES.items()}


def find_secrets(text: str):
    """Yield (pattern_name, matched_text) for each pattern found in the text (first hit per pattern)."""
    for name, regex in PATTERNS.items():
        match = regex.search(text)
        if match:
            yield name, match.group(0)
