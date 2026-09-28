/* =========================
   CURSOR
   ========================= */

function updateCursor(e, canvasRect) {

    const cursor =
        $('cursorCoords');

    if (
        typeof isMapLayerVisible === 'function' &&
        !isMapLayerVisible('cursorCoords')
    ) {
        if (cursor) {
            setStyle(cursor, 'display', 'none');
        }

        return;
    }

    const rect =
        canvasRect ||
        c.getBoundingClientRect();

    const x =
        e.clientX -
        rect.left;

    const y =
        e.clientY -
        rect.top;

    const world =
        toWorld(
            x,
            y
        );

    const bounds =
        getViewBounds();

    if (
        world.x <
        bounds.minX ||
        world.x >
        bounds.maxX ||
        world.y <
        bounds.minY ||
        world.y >
        bounds.maxY
    ) {

        setStyle(
            $('cursorCoords'),
            'display',
            'none'
        );

        return;
    }

    if (!cursor) {
        return;
    }

    setStyle(
        cursor,
        'display',
        'block'
    );

    setStyle(
        cursor,
        'left',
        `${x + 14}px`
    );

    setStyle(
        cursor,
        'top',
        `${y + 14}px`
    );

    setText(
        cursor.querySelector(
            '.cursor-x'
        ),
        `x${formatGameCoordinate(world.x)}`
    );

    setText(
        cursor.querySelector(
            '.cursor-y'
        ),
        `y${formatGameCoordinate(world.y)}`
    );
}
