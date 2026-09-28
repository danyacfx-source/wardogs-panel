/* =========================
   THEME
   ========================= */

function getTheme() {

    const saved =
        localStorage.getItem(
            'wardogs-theme'
        );

    if (
        saved === 'light' ||
        saved === 'dark'
    ) {
        return saved;
    }

    return 'dark';
}

function loadTheme() {
    applyTheme(
        getTheme()
    );
}

function applyTheme(theme) {

    const root =
        document.documentElement;

    const isLight =
        theme === 'light';

    if (isLight) {
        root.dataset.theme =
            'light';
    } else {
        delete root.dataset.theme;
    }

    localStorage.setItem(
        'wardogs-theme',
        isLight
            ? 'light'
            : 'dark'
    );

    updateThemeButton();
    draw();
}

function updateThemeButton() {

    const icon =
        $('themeIcon');

    if (!icon) {
        return;
    }

    const isLight =
        document.documentElement
            .dataset.theme === 'light';

    icon.textContent =
        isLight
            ? '☾'
            : '☼';

    const label =
        typeof tr === 'function'
            ? tr(
                isLight
                    ? 'switchToDarkTheme'
                    : 'switchToLightTheme'
            )
            : (
                isLight
                    ? 'Switch to dark theme'
                    : 'Switch to light theme'
            );

    $('themeToggle').setAttribute(
        'aria-label',
        label
    );

    $('themeToggle').title =
        label;
}

function toggleTheme() {

    const current =
        document.documentElement
            .dataset.theme === 'light'
            ? 'light'
            : 'dark';

    applyTheme(
        current === 'light'
            ? 'dark'
            : 'light'
    );
}
