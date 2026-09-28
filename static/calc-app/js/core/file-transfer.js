/* =========================
   JSON FILE TRANSFER
   ========================= */

const WARDOGS_JSON_MAX_BYTES = 1024 * 1024;

function wardogsExportTimestamp() {
    return new Date()
        .toISOString()
        .replace(/[:.]/g, '-')
        .replace('T', '_')
        .replace('Z', '');
}

function sanitizeWardogsFilenamePart(
    value,
    fallback = 'export'
) {
    const normalized = String(value || '')
        .trim()
        .replace(/[\\/:*?"<>|]+/g, '-')
        .replace(/\s+/g, '-')
        .replace(/-+/g, '-')
        .replace(/^-|-$/g, '')
        .slice(0, 80);

    return normalized || fallback;
}

function downloadWardogsJson(
    filename,
    payload
) {
    const json = JSON.stringify(
        payload,
        null,
        2
    );

    const blob = new Blob(
        [json],
        {
            type: 'application/json;charset=utf-8'
        }
    );

    const url = URL.createObjectURL(
        blob
    );

    const link =
        document.createElement('a');

    link.href = url;
    link.download = filename;
    link.style.display = 'none';

    document.body.appendChild(link);
    link.click();
    link.remove();

    window.setTimeout(
        () => URL.revokeObjectURL(url),
        0
    );
}

function selectWardogsJsonFile() {
    return new Promise(resolve => {
        const input =
            document.createElement('input');

        let settled = false;

        const finish = file => {
            if (settled) {
                return;
            }

            settled = true;
            window.removeEventListener(
                'focus',
                handleWindowFocus
            );
            input.remove();
            resolve(file || null);
        };

        const handleWindowFocus = () => {
            window.setTimeout(
                () => {
                    if (
                        !settled &&
                        !input.files?.length
                    ) {
                        finish(null);
                    }
                },
                250
            );
        };

        input.type = 'file';
        input.accept =
            '.json,application/json,text/json';
        input.style.display = 'none';

        input.addEventListener(
            'change',
            () => finish(
                input.files?.[0] || null
            ),
            { once: true }
        );

        input.addEventListener(
            'cancel',
            () => finish(null),
            { once: true }
        );

        window.addEventListener(
            'focus',
            handleWindowFocus,
            { once: true }
        );

        document.body.appendChild(input);
        input.click();
    });
}

async function readWardogsJsonFile(file) {
    if (!file) {
        return null;
    }

    if (!Number.isFinite(file.size) || file.size > WARDOGS_JSON_MAX_BYTES) {
        throw new Error('wardogs-json-file-too-large');
    }

    const text = await file.text();
    return JSON.parse(text);
}
