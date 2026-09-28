/* =========================
   COORDINATE COPY / PASTE
   ========================= */

const COORDINATE_FEEDBACK_DELAY = 1100;

function parseSharedCoordinates(value) {
    const text = String(value ?? '').trim();

    if (!text) {
        return null;
    }

    const numberPattern =
        '[+-]?\\d+(?:[\\.,]\\d+)?';

    const xMatch = text.match(
        new RegExp(
            `(?:^|[^a-z])x\\s*[:=]?\\s*(${numberPattern})`,
            'i'
        )
    );

    const yMatch = text.match(
        new RegExp(
            `(?:^|[^a-z])y\\s*[:=]?\\s*(${numberPattern})`,
            'i'
        )
    );

    const parseNumber = raw =>
        Number(
            String(raw)
                .replace(',', '.')
        );

    if (xMatch && yMatch) {
        const x = parseNumber(xMatch[1]);
        const y = parseNumber(yMatch[1]);

        return (
            Number.isFinite(x) &&
            Number.isFinite(y)
        )
            ? { x, y }
            : null;
    }

    const numbers = text.match(
        new RegExp(numberPattern, 'g')
    );

    if (!numbers || numbers.length !== 2) {
        return null;
    }

    const x = parseNumber(numbers[0]);
    const y = parseNumber(numbers[1]);

    return (
        Number.isFinite(x) &&
        Number.isFinite(y)
    )
        ? { x, y }
        : null;
}

function getSharedCoordinateText(type) {
    const point = S[type];

    if (!point) {
        return '';
    }

    return (
        `x${formatGameCoordinate(point.x)}, ` +
        `y${formatGameCoordinate(point.y)}`
    );
}

function coordinateActionButton(type, action) {
    const prefix =
        type === 'origin'
            ? 'Origin'
            : 'Target';

    const suffix =
        action === 'copy'
            ? 'Copy'
            : 'Paste';

    return $(`coordinate${prefix}${suffix}`);
}

function flashCoordinateAction(
    type,
    action,
    key
) {
    const button =
        coordinateActionButton(
            type,
            action
        );

    if (!button) {
        return;
    }

    button.textContent = tr(key);

    window.setTimeout(
        () => {
            if (!button.isConnected) {
                return;
            }

            button.textContent = tr(
                action === 'copy'
                    ? 'copyCoordinates'
                    : 'pasteCoordinates'
            );
        },
        COORDINATE_FEEDBACK_DELAY
    );
}

async function copyPointCoordinates(type) {
    const text =
        getSharedCoordinateText(type);

    if (!text) {
        return;
    }

    try {
        if (
            !navigator.clipboard ||
            typeof navigator.clipboard.writeText !== 'function'
        ) {
            throw new Error(
                'Clipboard write is unavailable'
            );
        }

        await navigator.clipboard.writeText(text);

        flashCoordinateAction(
            type,
            'copy',
            'coordinatesCopied'
        );

    } catch (_) {
        window.prompt(
            tr('copyCoordinatesPrompt'),
            text
        );
    }
}

async function readCoordinateClipboard() {
    if (
        navigator.clipboard &&
        typeof navigator.clipboard.readText === 'function'
    ) {
        try {
            const value =
                await navigator.clipboard.readText();

            if (value) {
                return value;
            }
        } catch (_) {
            // Fall back to a manual paste prompt below.
        }
    }

    return window.prompt(
        tr('pasteCoordinatesPrompt'),
        ''
    );
}

function applySharedCoordinates(
    type,
    coordinates
) {
    if (!coordinates) {
        return false;
    }

    const xInput =
        type === 'origin'
            ? $('ox')
            : $('tx');

    const yInput =
        type === 'origin'
            ? $('oy')
            : $('ty');

    if (!xInput || !yInput) {
        return false;
    }

    xInput.value = coordinates.x;
    yInput.value = coordinates.y;

    inputPoint(type);

    renderSavedTargets();

    return true;
}

async function pastePointCoordinates(type) {
    const text = await readCoordinateClipboard();

    if (text === null) {
        return;
    }

    const coordinates =
        parseSharedCoordinates(text);

    if (!coordinates) {
        flashCoordinateAction(
            type,
            'paste',
            'invalidCoordinates'
        );
        return;
    }

    if (
        applySharedCoordinates(
            type,
            coordinates
        )
    ) {
        flashCoordinateAction(
            type,
            'paste',
            'coordinatesPasted'
        );
    }
}
