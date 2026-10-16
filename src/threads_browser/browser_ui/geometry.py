"""Shared viewport and nested-scrollport geometry for browser UI reads."""

_SCROLLPORT_GEOMETRY_JS = """const clipFor = element => {
    const clip = {
        left: 0, top: 0,
        right: window.innerWidth, bottom: window.innerHeight,
    };
    for (let parent = element.parentElement; parent;
         parent = parent.parentElement) {
        // The document root is represented by the viewport above. Its rect
        // moves with document scrolling and would incorrectly shrink the clip.
        if (parent === document.body || parent === document.documentElement) {
            continue;
        }
        const style = getComputedStyle(parent);
        const clipX = style.overflowX !== "visible";
        const clipY = style.overflowY !== "visible";
        if (!clipX && !clipY) continue;
        const bounds = parent.getBoundingClientRect();
        const left = bounds.left + parent.clientLeft;
        const top = bounds.top + parent.clientTop;
        if (clipX) {
            clip.left = Math.max(clip.left, left);
            clip.right = Math.min(clip.right, left + parent.clientWidth);
        }
        if (clipY) {
            clip.top = Math.max(clip.top, top);
            clip.bottom = Math.min(clip.bottom, top + parent.clientHeight);
        }
    }
    return clip;
};
const geometryFor = element => {
    const rect = element.getBoundingClientRect();
    const clip = clipFor(element);
    const visible = {
        left: Math.max(rect.left, clip.left),
        top: Math.max(rect.top, clip.top),
        right: Math.min(rect.right, clip.right),
        bottom: Math.min(rect.bottom, clip.bottom),
    };
    return {
        rect, clip, visible,
        intersects: visible.right > visible.left && visible.bottom > visible.top,
    };
};
const scrollportBoundsFor = element => {
    const documentSurface = element === document.scrollingElement;
    const clip = clipFor(element);
    const rect = element.getBoundingClientRect();
    const content = documentSurface
        ? {
            left: 0, top: 0,
            right: window.innerWidth, bottom: window.innerHeight,
        }
        : {
            left: rect.left + element.clientLeft,
            top: rect.top + element.clientTop,
            right: rect.left + element.clientLeft + element.clientWidth,
            bottom: rect.top + element.clientTop + element.clientHeight,
        };
    return {
        documentSurface,
        left: Math.max(clip.left, content.left),
        top: Math.max(clip.top, content.top),
        right: Math.min(clip.right, content.right),
        bottom: Math.min(clip.bottom, content.bottom),
    };
};
const isScrollable = element => {
    if (!element || element === document.body ||
        element === document.documentElement) return false;
    const style = getComputedStyle(element);
    return element.scrollHeight > element.clientHeight + 1 &&
        ["auto", "scroll", "overlay"].includes(style.overflowY);
};
const keyFor = element => {
    if (!element || element === document.scrollingElement) return "DOCUMENT";
    const path = [];
    for (let current = element; current && current !== document.body;
         current = current.parentElement) {
        let siblingIndex = 0;
        for (const sibling of current.parentElement?.children || []) {
            if (sibling.tagName !== current.tagName) continue;
            if (sibling === current) break;
            siblingIndex++;
        }
        path.push(`${current.tagName.toLowerCase()}:${siblingIndex}`);
    }
    return path.reverse().join("/");
};
const scrollMetrics = element => {
    let scroller = element;
    while (scroller && !isScrollable(scroller)) scroller = scroller.parentElement;
    if (!scroller || scroller === document.body ||
        scroller === document.documentElement) {
        return {
            element: document.scrollingElement,
            offset: window.scrollY,
            top: window.scrollY,
            bottom: window.scrollY + window.innerHeight,
            scrollHeight: document.scrollingElement?.scrollHeight ?? 0,
            key: "DOCUMENT",
        };
    }
    const bounds = scroller.getBoundingClientRect();
    return {
        element: scroller,
        offset: scroller.scrollTop - bounds.top - scroller.clientTop,
        top: scroller.scrollTop,
        bottom: scroller.scrollTop + scroller.clientHeight,
        scrollHeight: scroller.scrollHeight,
        key: keyFor(scroller),
    };
};"""


def with_scrollport_geometry(source: str) -> str:
    """Insert shared geometry helpers into a browser-side expression."""
    return source.replace("/*SCROLLPORT_GEOMETRY*/", _SCROLLPORT_GEOMETRY_JS)
