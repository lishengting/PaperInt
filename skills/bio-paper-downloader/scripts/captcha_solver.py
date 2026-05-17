"""
Shared captcha solver using 2Captcha service.

Handles Cloudflare Turnstile (simple + challenge page) and reCAPTCHA v2/v3.
All solving calls are synchronous and run via asyncio.to_thread().

The 2Captcha API key is passed as a parameter from config.yaml (download.twocaptcha_api_key).
"""

import asyncio
import os
import sys

# Timeout for a single captcha solving attempt (2Captcha polling)
CAPTCHA_TIMEOUT = 45  # seconds

# JavaScript to intercept Cloudflare Turnstile render params on challenge pages
# Injects before page load via page.add_init_script()
INTERCEPT_TURNSTILE_SCRIPT = """
window.__cfTurnstileParams = null;
window.__cfCallback = null;
const _cfPoll = setInterval(() => {
    if (window.turnstile) {
        clearInterval(_cfPoll);
        const _origRender = window.turnstile.render;
        window.turnstile.render = (a, b) => {
            window.__cfTurnstileParams = {
                sitekey: b.sitekey,
                pageurl: window.location.href,
                data: b.cData,
                pagedata: b.chlPageData,
                action: b.action,
                userAgent: navigator.userAgent,
            };
            window.__cfCallback = b.callback;
            return _origRender ? _origRender(a, b) : undefined;
        };
    }
}, 50);
"""


def _get_solver(api_key):
    """Return a TwoCaptcha solver instance, or None if API key is missing."""
    if not api_key:
        print("  [captcha] twocaptcha_api_key not set, skipping captcha solving",
              file=sys.stderr)
        return None
    try:
        from twocaptcha import TwoCaptcha
        return TwoCaptcha(apiKey=api_key)
    except ImportError:
        print("  [captcha] twocaptcha package not installed", file=sys.stderr)
        return None


async def _detect_captcha_type(page) -> str | None:
    """Detect what kind of captcha is present on the page. Returns type or None."""
    result = await page.evaluate("""() => {
        // Cloudflare Turnstile widget
        if (document.querySelector('.cf-turnstile') ||
            document.querySelector('div[data-sitekey]') &&
            document.querySelector('iframe[src*="challenges.cloudflare.com"]')) {
            return 'turnstile';
        }
        // reCAPTCHA v2 (checkbox)
        if (document.querySelector('.g-recaptcha') ||
            document.querySelector('iframe[src*="recaptcha/api2"]')) {
            return 'recaptcha_v2';
        }
        // reCAPTCHA v3 (invisible badge)
        if (document.querySelector('.grecaptcha-badge') ||
            document.querySelector('script[src*="recaptcha/api.js"]') ||
            document.querySelector('script[src*="recaptcha/enterprise.js"]')) {
            return 'recaptcha_v3';
        }
        return null;
    }""")


async def _extract_sitekey(page, captcha_type: str) -> str | None:
    """Extract the sitekey for the detected captcha type."""
    if captcha_type == 'turnstile':
        return await page.evaluate("""() => {
            const el = document.querySelector('.cf-turnstile');
            return el ? el.getAttribute('data-sitekey') : null;
        }""")
    elif captcha_type in ('recaptcha_v2', 'recaptcha_v3'):
        return await page.evaluate("""() => {
            const el = document.querySelector('.g-recaptcha');
            return el ? el.getAttribute('data-sitekey') : null;
        }""")
    return None


def _solve_turnstile_sync(api_key, sitekey, pageurl, params=None):
    """Synchronous: call 2Captcha Turnstile API. Returns token or None."""
    from twocaptcha import TwoCaptcha
    solver = TwoCaptcha(apiKey=api_key)
    if params:
        result = solver.turnstile(
            sitekey=params.get('sitekey', sitekey),
            url=params.get('pageurl', pageurl),
            action=params.get('action'),
            data=params.get('data'),
            pagedata=params.get('pagedata'),
            useragent=params.get('userAgent'),
        )
    else:
        result = solver.turnstile(sitekey=sitekey, url=pageurl)
    return result.get('code')


def _solve_recaptcha_sync(api_key, sitekey, pageurl, captcha_type):
    """Synchronous: call 2Captcha reCAPTCHA API. Returns token or None."""
    from twocaptcha import TwoCaptcha
    solver = TwoCaptcha(apiKey=api_key)
    version = 'V3' if captcha_type == 'recaptcha_v3' else None
    kwargs = {'sitekey': sitekey, 'url': pageurl}
    if version:
        kwargs['version'] = version
    result = solver.recaptcha(**kwargs)
    return result.get('code')


async def _solve_turnstile(page, api_key, log_prefix: str) -> str | None:
    """Simple Turnstile: extract sitekey from DOM widget, solve, submit token."""
    sitekey = await _extract_sitekey(page, 'turnstile')
    if not sitekey:
        print(f"{log_prefix} Turnstile sitekey not found in DOM", file=sys.stderr)
        return None

    if not api_key:
        return None

    pageurl = page.url
    print(f"{log_prefix} Solving Turnstile (sitekey={sitekey[:20]}...)", file=sys.stderr)
    token = await asyncio.to_thread(_solve_turnstile_sync, api_key, sitekey, pageurl)
    if not token:
        print(f"{log_prefix} Turnstile solving returned no token", file=sys.stderr)
        return None

    # Submit token to the hidden input field
    submitted = await page.evaluate("""([token]) => {
        const input = document.querySelector('input[name="cf-turnstile-response"]');
        if (input) {
            input.value = token;
            input.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }
        return false;
    }""", [token])

    if submitted:
        print(f"{log_prefix} Turnstile token submitted", file=sys.stderr)
        return token
    else:
        print(f"{log_prefix} Turnstile input field not found", file=sys.stderr)
        return None


async def _solve_turnstile_challenge(page, api_key, log_prefix: str) -> str | None:
    """Turnstile Challenge Page: intercept render params, solve, call callback."""
    if not api_key:
        return None

    # Inject interception script before next page load
    await page.add_init_script(INTERCEPT_TURNSTILE_SCRIPT)
    await page.reload(wait_until='domcontentloaded')

    # Wait for the interception to capture turnstile.render parameters
    try:
        await page.wait_for_function(
            'window.__cfTurnstileParams !== null',
            timeout=CAPTCHA_TIMEOUT * 1000)
    except Exception:
        print(f"{log_prefix} Timed out waiting for Turnstile params", file=sys.stderr)
        return None

    params = await page.evaluate('() => window.__cfTurnstileParams')
    if not params:
        print(f"{log_prefix} Failed to capture Turnstile params", file=sys.stderr)
        return None

    print(f"{log_prefix} Solving Turnstile challenge (sitekey={params.get('sitekey', '?')[:20]}...)",
          file=sys.stderr)
    token = await asyncio.to_thread(_solve_turnstile_sync, api_key,
                                     params.get('sitekey', ''),
                                     params.get('pageurl', page.url),
                                     params=params)
    if not token:
        print(f"{log_prefix} Turnstile challenge solving returned no token", file=sys.stderr)
        return None

    # Execute the captured callback with the solved token
    await page.evaluate('(token) => { window.__cfCallback(token); }', token)
    print(f"{log_prefix} Turnstile challenge token submitted via callback", file=sys.stderr)
    return token


async def _solve_recaptcha(page, api_key, captcha_type: str, log_prefix: str) -> str | None:
    """reCAPTCHA v2/v3: extract sitekey, solve, submit token."""
    sitekey = await _extract_sitekey(page, captcha_type)
    if not sitekey:
        print(f"{log_prefix} reCAPTCHA sitekey not found in DOM", file=sys.stderr)
        return None

    if not api_key:
        return None

    pageurl = page.url
    print(f"{log_prefix} Solving reCAPTCHA ({captcha_type}, sitekey={sitekey[:20]}...)",
          file=sys.stderr)
    token = await asyncio.to_thread(_solve_recaptcha_sync, api_key, sitekey,
                                     pageurl, captcha_type)
    if not token:
        print(f"{log_prefix} reCAPTCHA solving returned no token", file=sys.stderr)
        return None

    # Submit token to g-recaptcha-response textarea and trigger callback
    await page.evaluate("""([token]) => {
        // Set the hidden response field
        const ta = document.querySelector('#g-recaptcha-response') ||
                   document.querySelector('textarea[name="g-recaptcha-response"]');
        if (ta) {
            ta.value = token;
            ta.dispatchEvent(new Event('change', {bubbles: true}));
        }
        // Try calling recaptcha callbacks
        if (window.___grecaptcha_cfg && ___grecaptcha_cfg.clients) {
            for (const key of Object.keys(___grecaptcha_cfg.clients)) {
                const client = ___grecaptcha_cfg.clients[key];
                if (client && client.callback) {
                    client.callback(token);
                    break;
                }
            }
        }
    }""", [token])

    print(f"{log_prefix} reCAPTCHA token submitted", file=sys.stderr)
    return token


async def try_solve_captcha(page, captcha_enabled: bool, api_key: str = '',
                            log_prefix: str = '') -> bool:
    """Attempt to detect and solve a captcha on the current page.

    Returns True if a captcha was found AND successfully solved.
    Returns False if captcha is disabled, no captcha found, or solving failed.

    This function never raises — all errors are caught and logged.
    """
    if not captcha_enabled:
        return False

    if not api_key:
        return False

    # Detect captcha type
    try:
        captcha_type = await _detect_captcha_type(page)
    except Exception:
        return False

    if not captcha_type:
        return False

    print(f"{log_prefix} Captcha detected: {captcha_type}", file=sys.stderr)

    try:
        if captcha_type == 'turnstile':
            # Try simple mode first
            token = await _solve_turnstile(page, api_key, log_prefix)
            if not token:
                # Fall back to challenge-page mode
                token = await _solve_turnstile_challenge(page, api_key, log_prefix)
        elif captcha_type in ('recaptcha_v2', 'recaptcha_v3'):
            token = await _solve_recaptcha(page, api_key, captcha_type, log_prefix)
        else:
            token = None

        if token:
            # Wait a moment for the page to process the token
            await asyncio.sleep(3)
            return True

    except asyncio.TimeoutError:
        print(f"{log_prefix} Captcha solving timed out", file=sys.stderr)
    except Exception as e:
        print(f"{log_prefix} Captcha solving error: {e}", file=sys.stderr)

    return False