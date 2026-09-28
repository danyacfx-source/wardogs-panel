/* =========================
   MOBILE ROUTING
   ========================= */

(() => {
    const FORCE_DESKTOP_KEY =
        'wardogs-force-desktop';

    const params =
        new URLSearchParams(
            window.location.search
        );

    /*
     * The mobile UI exposes a "Desktop version" link
     * using ?desktop=1. Keep that preference only for
     * the current browser tab/session so mobile users
     * are not permanently locked out of auto-routing.
     */
    if (params.get('desktop') === '1') {
        try {
            sessionStorage.setItem(
                FORCE_DESKTOP_KEY,
                '1'
            );
        } catch (_) {
            // Storage access is optional.
        }

        params.delete('desktop');

        const cleanURL =
            new URL(
                window.location.href
            );

        cleanURL.search =
            params.toString();

        window.history.replaceState(
            null,
            '',
            cleanURL.href
        );

        return;
    }

    try {
        if (
            sessionStorage.getItem(
                FORCE_DESKTOP_KEY
            ) === '1'
        ) {
            return;
        }
    } catch (_) {
        // Continue with automatic detection.
    }

    const userAgent =
        navigator.userAgent ||
        '';

    const iPadDevice =
        /\biPad\b/i.test(
            userAgent
        ) ||
        (
            navigator.platform ===
                'MacIntel' &&
            Number(
                navigator.maxTouchPoints
            ) > 1
        ) ||
        (
            /Macintosh/i.test(
                userAgent
            ) &&
            Number(
                navigator.maxTouchPoints
            ) > 1
        );

    const mobileDevice =
        iPadDevice ||
        navigator.userAgentData
            ?.mobile === true ||
        (
            window.matchMedia(
                '(pointer: coarse)'
            ).matches &&
            window.matchMedia(
                '(max-width: 900px)'
            ).matches
        );

    if (!mobileDevice) {
        return;
    }

    const siteRoot =
        new URL(
            './',
            document.baseURI
        );

    const pageLanguage =
        document.documentElement
            .dataset.pageLanguage ||
        'en';

    const languagePath =
        pageLanguage === 'en'
            ? ''
            : `${pageLanguage}/`;

    const target =
        new URL(
            `mobile/${languagePath}`,
            siteRoot
        );

    target.search =
        window.location.search;

    target.hash =
        window.location.hash;

    window.location.replace(
        target.href
    );
})();
