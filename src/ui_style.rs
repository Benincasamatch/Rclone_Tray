use super::ids;
use crate::contracts::RunState;
use windows_sys::Win32::Foundation::{COLORREF, POINT, RECT, SIZE};
use windows_sys::Win32::Graphics::Gdi::*;
use windows_sys::Win32::UI::Controls::{
    DRAWITEMSTRUCT, ODS_DISABLED, ODS_FOCUS, ODS_GRAYED, ODS_HOTLIGHT,
    ODS_NOFOCUSRECT, ODS_SELECTED,
};

#[derive(Clone, Copy)]
pub(super) struct Palette {
    pub(super) bg: COLORREF,
    pub(super) sidebar: COLORREF,
    pub(super) panel: COLORREF,
    pub(super) input: COLORREF,
    pub(super) hover: COLORREF,
    pub(super) border: COLORREF,
    pub(super) text: COLORREF,
    pub(super) secondary: COLORREF,
    pub(super) muted: COLORREF,
    pub(super) disabled: COLORREF,
    pub(super) accent: COLORREF,
    pub(super) selected: COLORREF,
    pub(super) success: COLORREF,
    pub(super) warning: COLORREF,
    pub(super) error: COLORREF,
}

const fn rgb(hex: u32) -> COLORREF {
    ((hex >> 16) & 0xff) | (hex & 0xff00) | ((hex & 0xff) << 16)
}

pub(super) fn palette(dark: bool) -> Palette {
    if dark {
        Palette {
            bg: rgb(0x101114),
            sidebar: rgb(0x17191e),
            panel: rgb(0x1b1d24),
            input: rgb(0x16181d),
            hover: rgb(0x292b34),
            border: rgb(0x2b2d36),
            text: rgb(0xe6e7eb),
            secondary: rgb(0xb4b6c0),
            muted: rgb(0x858895),
            disabled: rgb(0x555863),
            accent: rgb(0xa78bfa),
            selected: rgb(0x2f2b42),
            success: rgb(0x65c99a),
            warning: rgb(0xe7b96a),
            error: rgb(0xe87979),
        }
    } else {
        Palette {
            bg: rgb(0xf4f5f8),
            sidebar: rgb(0xeceef3),
            panel: rgb(0xffffff),
            input: rgb(0xeceef4),
            hover: rgb(0xe5e7ef),
            border: rgb(0xdde0e8),
            text: rgb(0x242631),
            secondary: rgb(0x5b5f70),
            muted: rgb(0x777c8f),
            disabled: rgb(0xa7abb9),
            accent: rgb(0x795acc),
            selected: rgb(0xebe4fa),
            success: rgb(0x248058),
            warning: rgb(0x9a681d),
            error: rgb(0xba4545),
        }
    }
}

fn px(value: i32, dpi: u32) -> i32 {
    let dpi = if dpi == 0 { 96 } else { dpi };
    ((i64::from(value) * i64::from(dpi) + 48) / 96) as i32
}

fn mix(base: COLORREF, tint: COLORREF, percent: u32) -> COLORREF {
    let channel = |shift: u32| -> u32 {
        let a = (base >> shift) & 0xff;
        let b = (tint >> shift) & 0xff;
        (a * (100 - percent) + b * percent + 50) / 100
    };
    channel(0) | (channel(8) << 8) | (channel(16) << 16)
}

fn ink(fill: COLORREF) -> COLORREF {
    let brightness = (fill & 0xff) * 299
        + ((fill >> 8) & 0xff) * 587
        + ((fill >> 16) & 0xff) * 114;
    if brightness >= 150_000 { rgb(0x20192e) } else { rgb(0xffffff) }
}

fn inset(rect: RECT, amount: i32) -> RECT {
    RECT {
        left: rect.left + amount,
        top: rect.top + amount,
        right: rect.right - amount,
        bottom: rect.bottom - amount,
    }
}

struct SavedDc {
    hdc: HDC,
    saved: i32,
}

impl SavedDc {
    unsafe fn new(hdc: HDC) -> Option<Self> {
        if hdc.is_null() { return None; }
        let saved = SaveDC(hdc);
        (saved != 0).then_some(Self { hdc, saved })
    }
}

impl Drop for SavedDc {
    fn drop(&mut self) {
        unsafe { RestoreDC(self.hdc, self.saved); }
    }
}

struct SelectedPen {
    hdc: HDC,
    pen: HPEN,
    previous: HGDIOBJ,
}

impl SelectedPen {
    unsafe fn new(hdc: HDC, width: i32, color: COLORREF) -> Option<Self> {
        let pen = CreatePen(PS_SOLID, width.max(1), color);
        if pen.is_null() { return None; }
        let previous = SelectObject(hdc, pen);
        if previous.is_null() || previous as isize == -1 {
            DeleteObject(pen);
            return None;
        }
        Some(Self { hdc, pen, previous })
    }
}

impl Drop for SelectedPen {
    fn drop(&mut self) {
        unsafe {
            SelectObject(self.hdc, self.previous);
            DeleteObject(self.pen);
        }
    }
}

unsafe fn fill(hdc: HDC, rect: RECT, color: COLORREF) {
    if rect.right <= rect.left || rect.bottom <= rect.top { return; }
    SetDCBrushColor(hdc, color);
    FillRect(hdc, &rect, GetStockObject(DC_BRUSH));
}

unsafe fn rounded(
    hdc: HDC,
    rect: RECT,
    radius: i32,
    color: Option<COLORREF>,
    edge: Option<COLORREF>,
) {
    if rect.right <= rect.left || rect.bottom <= rect.top { return; }
    if let Some(color) = color {
        SelectObject(hdc, GetStockObject(DC_BRUSH));
        SetDCBrushColor(hdc, color);
    } else {
        SelectObject(hdc, GetStockObject(NULL_BRUSH));
    }
    if let Some(edge) = edge {
        SelectObject(hdc, GetStockObject(DC_PEN));
        SetDCPenColor(hdc, edge);
    } else {
        SelectObject(hdc, GetStockObject(NULL_PEN));
    }
    RoundRect(hdc, rect.left, rect.top, rect.right, rect.bottom, radius * 2, radius * 2);
}

// Every scalar is encoded as a whole; neither capacity truncation nor joining
// strings can leave an unmatched UTF-16 surrogate. Reserve ellipsis and NUL.
struct Utf16 {
    units: [u16; 2048],
    len: usize,
    truncated: bool,
}

impl Utf16 {
    fn new() -> Self {
        Self { units: [0; 2048], len: 0, truncated: false }
    }

    fn append(&mut self, value: &str) {
        if self.truncated { return; }
        for ch in value.chars() {
            let mut encoded = [0; 2];
            let units = ch.encode_utf16(&mut encoded);
            if self.len + units.len() > self.units.len() - 2 {
                self.units[self.len] = 0x2026;
                self.len += 1;
                self.truncated = true;
                break;
            }
            self.units[self.len..self.len + units.len()].copy_from_slice(units);
            self.len += units.len();
        }
        self.units[self.len] = 0;
    }
}

unsafe fn draw_text(
    hdc: HDC,
    mut rect: RECT,
    value: &Utf16,
    color: COLORREF,
    flags: u32,
) {
    if rect.right <= rect.left || rect.bottom <= rect.top { return; }
    SetTextColor(hdc, color);
    // DrawText's MODIFYSTRING mode writes beyond its input; this painter owns a
    // fixed immutable buffer and deliberately never exposes that mode.
    DrawTextW(hdc, value.units.as_ptr(), value.len as i32, &mut rect, flags & !DT_MODIFYSTRING);
}

pub(super) unsafe fn text(
    hdc: HDC,
    rect: RECT,
    value: &str,
    font: HFONT,
    color: COLORREF,
    flags: u32,
) {
    let Some(_dc) = SavedDc::new(hdc) else { return; };
    if !font.is_null() { SelectObject(hdc, font); }
    SetBkMode(hdc, TRANSPARENT as i32);
    let mut value_wide = Utf16::new();
    value_wide.append(value);
    draw_text(hdc, rect, &value_wide, color, flags);
}

pub(super) unsafe fn background(
    hdc: HDC,
    rect: RECT,
    dpi: u32,
    sidebar: i32,
    editor: bool,
    p: Palette,
) {
    let Some(_dc) = SavedDc::new(hdc) else { return; };
    fill(hdc, rect, p.bg);
    let divider = if editor {
        rect.left
    } else {
        (rect.left + px(sidebar.max(0), dpi)).min(rect.right)
    };
    let hairline = px(1, dpi).max(1);
    if !editor && divider > rect.left {
        fill(hdc, RECT { right: divider, ..rect }, p.sidebar);
        fill(hdc, RECT { left: divider - hairline, right: divider, ..rect }, p.border);
    }
    let header_y = rect.top + px(104, dpi);
    if header_y < rect.bottom {
        fill(hdc, RECT {
            left: rect.left,
            top: header_y,
            right: rect.right,
            bottom: (header_y + hairline).min(rect.bottom),
        }, p.border);
    }
    let footer_y = rect.bottom - px(36, dpi);
    if footer_y > header_y {
        fill(hdc, RECT {
            left: divider,
            top: footer_y,
            right: rect.right,
            bottom: footer_y + hairline,
        }, p.border);
    }
}

pub(super) unsafe fn border(hdc: HDC, rect: RECT, dpi: u32, color: COLORREF) {
    let Some(_dc) = SavedDc::new(hdc) else { return; };
    let Some(_pen) = SelectedPen::new(hdc, px(1, dpi), color) else { return; };
    SelectObject(hdc, GetStockObject(NULL_BRUSH));
    let rect = inset(rect, px(1, dpi) / 2);
    if rect.right <= rect.left || rect.bottom <= rect.top { return; }
    let diameter = px(12, dpi);
    RoundRect(hdc, rect.left, rect.top, rect.right, rect.bottom, diameter, diameter);
}

#[derive(Clone, Copy)]
enum Icon {
    Drive, Monitor, Sliders, Info, Sidebar, Refresh, Close, Exit, Add,
    Edit, Delete, Play, Stop, PlayAll, StopAll, Folder, Document, Save,
    Undo, Copy, Search, Link,
}

fn icon_for(id: u16) -> Option<Icon> {
    use ids::*;
    Some(match id {
        NAV_MOUNTS | MOUNT_LIST => Icon::Drive,
        NAV_SERVICE => Icon::Monitor,
        NAV_SETTINGS => Icon::Sliders,
        NAV_ABOUT => Icon::Info,
        TOGGLE_NAV => Icon::Sidebar,
        RELOAD | REFRESH | DETECT | ABOUT_REFRESH | EDITOR_RELOAD => Icon::Refresh,
        CLOSE | SERVICE_CANCEL | SETTINGS_CANCEL | EDITOR_CANCEL | CLEAR_FILTER => Icon::Close,
        EXIT | ABOUT_EXIT => Icon::Exit,
        ADD => Icon::Add,
        EDIT => Icon::Edit,
        DELETE => Icon::Delete,
        START | GUI_START => Icon::Play,
        STOP | GUI_STOP => Icon::Stop,
        START_ALL => Icon::PlayAll,
        STOP_ALL => Icon::StopAll,
        OPEN | BROWSE_EXE | BROWSE_CONF | BROWSE_POINT | LOG_DIR | CONF_DIR => Icon::Folder,
        LOG | GUI_LOG | OPEN_CONF | APP_CONFIG | ABOUT_CONF | EDITOR_CONF => Icon::Document,
        SERVICE_SAVE | SETTINGS_SAVE | EDITOR_SAVE => Icon::Save,
        SERVICE_REBASE | SETTINGS_REBASE | EDITOR_REBASE => Icon::Undo,
        COPY => Icon::Copy,
        AUTO_DISCOVER => Icon::Search,
        GUI_OPEN | HELP | WINFSP_HELP => Icon::Link,
        _ => return None,
    })
}

unsafe fn path<const N: usize>(hdc: HDC, x: i32, y: i32, dpi: u32, points: [(i32, i32); N]) {
    let points = points.map(|(dx, dy)| POINT { x: x + px(dx, dpi), y: y + px(dy, dpi) });
    Polyline(hdc, points.as_ptr(), N as i32);
}

unsafe fn icon_box(hdc: HDC, x: i32, y: i32, dpi: u32, bounds: [i32; 4], radius: i32) {
    RoundRect(
        hdc, x + px(bounds[0], dpi), y + px(bounds[1], dpi),
        x + px(bounds[2], dpi), y + px(bounds[3], dpi),
        px(radius * 2, dpi), px(radius * 2, dpi),
    );
}

unsafe fn icon_circle(hdc: HDC, x: i32, y: i32, dpi: u32, bounds: [i32; 4]) {
    Ellipse(hdc, x + px(bounds[0], dpi), y + px(bounds[1], dpi),
        x + px(bounds[2], dpi), y + px(bounds[3], dpi));
}

unsafe fn icon(hdc: HDC, kind: Icon, x: i32, y: i32, dpi: u32, color: COLORREF) {
    let Some(_dc) = SavedDc::new(hdc) else { return; };
    let dpi_value = if dpi == 0 { 96 } else { dpi };
    let width = ((u64::from(dpi_value) * 3 + 96) / 192) as i32;
    let Some(_pen) = SelectedPen::new(hdc, width, color) else { return; };
    SelectObject(hdc, GetStockObject(NULL_BRUSH));
    match kind {
        Icon::Drive => {
            icon_box(hdc, x, y, dpi, [2, 5, 18, 16], 2);
            path(hdc, x, y, dpi, [(3, 11), (17, 11)]);
            path(hdc, x, y, dpi, [(13, 14), (15, 14)]);
        }
        Icon::Monitor => {
            icon_box(hdc, x, y, dpi, [2, 3, 18, 14], 2);
            path(hdc, x, y, dpi, [(10, 14), (10, 17)]);
            path(hdc, x, y, dpi, [(6, 17), (14, 17)]);
        }
        Icon::Sliders => {
            for (cx, top) in [(4, 5), (10, 11), (16, 7)] {
                path(hdc, x, y, dpi, [(cx, 2), (cx, top)]);
                path(hdc, x, y, dpi, [(cx, top + 4), (cx, 18)]);
                icon_box(hdc, x, y, dpi, [cx - 2, top, cx + 2, top + 4], 1);
            }
        }
        Icon::Info => {
            icon_circle(hdc, x, y, dpi, [2, 2, 18, 18]);
            path(hdc, x, y, dpi, [(10, 9), (10, 14)]);
            path(hdc, x, y, dpi, [(10, 6), (10, 7)]);
        }
        Icon::Sidebar => {
            icon_box(hdc, x, y, dpi, [2, 3, 18, 17], 2);
            path(hdc, x, y, dpi, [(7, 4), (7, 16)]);
            path(hdc, x, y, dpi, [(13, 7), (10, 10), (13, 13)]);
        }
        Icon::Refresh => {
            path(hdc, x, y, dpi, [(17, 8), (15, 4), (10, 3), (5, 5), (3, 10), (5, 15), (10, 17), (15, 15)]);
            path(hdc, x, y, dpi, [(17, 3), (17, 8), (12, 8)]);
        }
        Icon::Close => {
            path(hdc, x, y, dpi, [(5, 5), (15, 15)]);
            path(hdc, x, y, dpi, [(15, 5), (5, 15)]);
        }
        Icon::Exit => {
            path(hdc, x, y, dpi, [(9, 3), (3, 3), (3, 17), (9, 17)]);
            path(hdc, x, y, dpi, [(8, 10), (18, 10)]);
            path(hdc, x, y, dpi, [(14, 6), (18, 10), (14, 14)]);
        }
        Icon::Add => {
            path(hdc, x, y, dpi, [(10, 3), (10, 17)]);
            path(hdc, x, y, dpi, [(3, 10), (17, 10)]);
        }
        Icon::Edit => {
            path(hdc, x, y, dpi, [(3, 17), (4, 12), (14, 2), (18, 6), (8, 16), (3, 17)]);
            path(hdc, x, y, dpi, [(12, 4), (16, 8)]);
        }
        Icon::Delete => {
            path(hdc, x, y, dpi, [(3, 5), (17, 5)]);
            path(hdc, x, y, dpi, [(7, 5), (7, 2), (13, 2), (13, 5)]);
            path(hdc, x, y, dpi, [(5, 5), (6, 17), (14, 17), (15, 5)]);
            path(hdc, x, y, dpi, [(8, 8), (8, 14)]);
            path(hdc, x, y, dpi, [(12, 8), (12, 14)]);
        }
        Icon::Play => path(hdc, x, y, dpi, [(6, 3), (17, 10), (6, 17), (6, 3)]),
        Icon::Stop => icon_box(hdc, x, y, dpi, [4, 4, 16, 16], 2),
        Icon::PlayAll => {
            path(hdc, x, y, dpi, [(3, 4), (12, 10), (3, 16), (3, 4)]);
            path(hdc, x, y, dpi, [(11, 4), (19, 10), (11, 16)]);
        }
        Icon::StopAll => {
            icon_box(hdc, x, y, dpi, [2, 5, 12, 15], 1);
            path(hdc, x, y, dpi, [(15, 5), (18, 5), (18, 15), (15, 15)]);
        }
        Icon::Folder => {
            path(hdc, x, y, dpi, [(2, 7), (2, 4), (8, 4), (10, 7), (18, 7), (16, 16), (3, 16), (2, 7), (18, 7)]);
        }
        Icon::Document => {
            path(hdc, x, y, dpi, [(12, 2), (4, 2), (4, 18), (16, 18), (16, 6), (12, 2), (12, 6), (16, 6)]);
            path(hdc, x, y, dpi, [(7, 10), (13, 10)]);
            path(hdc, x, y, dpi, [(7, 14), (13, 14)]);
        }
        Icon::Save => {
            path(hdc, x, y, dpi, [(3, 3), (14, 3), (17, 6), (17, 17), (3, 17), (3, 3)]);
            path(hdc, x, y, dpi, [(7, 3), (7, 8), (13, 8), (13, 3)]);
            icon_box(hdc, x, y, dpi, [6, 12, 14, 17], 1);
        }
        Icon::Undo => {
            path(hdc, x, y, dpi, [(7, 3), (3, 7), (7, 11)]);
            path(hdc, x, y, dpi, [(3, 7), (12, 7), (16, 10), (16, 14), (13, 17), (9, 17)]);
        }
        Icon::Copy => {
            icon_box(hdc, x, y, dpi, [7, 6, 17, 18], 1);
            path(hdc, x, y, dpi, [(13, 3), (3, 3), (3, 14), (5, 14)]);
        }
        Icon::Search => {
            icon_circle(hdc, x, y, dpi, [2, 2, 14, 14]);
            path(hdc, x, y, dpi, [(12, 12), (18, 18)]);
        }
        Icon::Link => {
            path(hdc, x, y, dpi, [(9, 4), (3, 4), (3, 17), (16, 17), (16, 11)]);
            path(hdc, x, y, dpi, [(11, 2), (18, 2), (18, 9)]);
            path(hdc, x, y, dpi, [(18, 2), (9, 11)]);
        }
    }
}

pub(super) unsafe fn button(
    item: &DRAWITEMSTRUCT,
    p: Palette,
    dpi: u32,
    font: HFONT,
    caption: &str,
    active: bool,
    hot: bool,
    check: Option<bool>,
) {
    let hdc = item.hDC;
    let Some(_dc) = SavedDc::new(hdc) else { return; };
    let rect = item.rcItem;
    if rect.right <= rect.left || rect.bottom <= rect.top { return; }
    IntersectClipRect(hdc, rect.left, rect.top, rect.right, rect.bottom);
    if !font.is_null() { SelectObject(hdc, font); }
    SetBkMode(hdc, TRANSPARENT as i32);
    let id = item.CtlID as u16;
    let navigation = matches!(id, ids::NAV_MOUNTS | ids::NAV_SERVICE | ids::NAV_SETTINGS | ids::NAV_ABOUT);
    let sidebar_button = navigation || matches!(id, ids::TOGGLE_NAV | ids::EXIT);
    let disabled = item.itemState & (ODS_DISABLED | ODS_GRAYED) != 0;
    let pressed = !disabled && item.itemState & ODS_SELECTED != 0;
    let hot = !disabled && (hot || item.itemState & ODS_HOTLIGHT != 0);
    let focused = !disabled && item.itemState & (ODS_FOCUS | ODS_NOFOCUSRECT) == ODS_FOCUS;
    let canvas = if sidebar_button { p.sidebar } else { p.bg };
    fill(hdc, rect, canvas);
    let face = inset(rect, px(1, dpi).max(1));

    if let Some(checked) = check {
        if hot || pressed { rounded(hdc, face, px(6, dpi), Some(p.input), None); }
        let size = px(16, dpi);
        let left = rect.left + px(4, dpi);
        let top = rect.top + (rect.bottom - rect.top - size) / 2;
        let box_rect = RECT { left, top, right: left + size, bottom: top + size };
        let check_fill = if disabled { p.input } else if checked { p.accent } else { p.input };
        let check_edge = if disabled { p.border } else if checked || focused { p.accent } else { p.muted };
        rounded(hdc, box_rect, px(3, dpi), Some(check_fill), Some(check_edge));
        if checked {
            let check_color = if disabled { p.disabled } else { ink(p.accent) };
            if let Some(_pen) = SelectedPen::new(hdc, px(2, dpi), check_color) {
                path(hdc, left, top, dpi, [(3, 8), (6, 11), (12, 4)]);
            }
        }
        let mut wide = Utf16::new();
        wide.append(caption);
        draw_text(hdc, RECT { left: left + size + px(10, dpi), right: rect.right - px(4, dpi), ..rect },
            &wide, if disabled { p.disabled } else { p.secondary }, DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
        if focused { rounded(hdc, face, px(6, dpi), None, Some(p.accent)); }
        return;
    }

    let primary = matches!(id, ids::ADD | ids::SERVICE_SAVE | ids::SETTINGS_SAVE | ids::EDITOR_SAVE);
    let danger = matches!(id, ids::DELETE | ids::EXIT | ids::ABOUT_EXIT);
    let ghost = sidebar_button || matches!(id, ids::CLOSE | ids::CLEAR_FILTER | ids::SERVICE_CANCEL | ids::SETTINGS_CANCEL | ids::EDITOR_CANCEL);
    let mut foreground = if navigation && active { p.accent } else if danger { p.error } else { p.secondary };
    let mut face_color = if navigation && active {
        Some(p.selected)
    } else if primary {
        foreground = ink(p.accent);
        Some(p.accent)
    } else if ghost {
        None
    } else {
        Some(p.panel)
    };
    let mut edge = if ghost || primary { None } else { Some(p.border) };
    if disabled {
        foreground = p.disabled;
        face_color = if ghost { None } else { Some(p.input) };
        edge = if ghost { None } else { Some(p.border) };
    } else if pressed {
        face_color = Some(if primary { mix(p.accent, p.bg, 18) }
            else if navigation && active { mix(p.selected, p.accent, 12) }
            else if danger { mix(canvas, p.error, 16) } else { p.input });
    } else if hot {
        face_color = Some(if primary { mix(p.accent, rgb(0xffffff), 14) }
            else if navigation && active { mix(p.selected, p.accent, 8) }
            else if danger { mix(canvas, p.error, 10) } else { p.hover });
        if !danger && !primary && !(navigation && active) { foreground = p.text; }
    }
    if focused { edge = Some(p.accent); }
    rounded(hdc, face, px(7, dpi), face_color, edge);

    let kind = icon_for(id);
    let width = rect.right - rect.left;
    let icon_only = sidebar_button && width < px(80, dpi);
    let shift = if pressed { px(1, dpi).max(1) } else { 0 };
    let icon_size = px(20, dpi);
    let icon_y = rect.top + (rect.bottom - rect.top - icon_size) / 2 + shift;
    if icon_only {
        if let Some(kind) = kind {
            icon(hdc, kind, rect.left + (width - icon_size) / 2 + shift, icon_y, dpi, foreground);
        }
        return;
    }
    let mut wide = Utf16::new();
    wide.append(caption);
    let padding = px(12, dpi);
    let gap = if kind.is_some() && !caption.is_empty() { px(8, dpi) } else { 0 };
    let icon_width = if kind.is_some() { icon_size + gap } else { 0 };
    let mut measured = SIZE::default();
    GetTextExtentPoint32W(hdc, wide.units.as_ptr(), wide.len as i32, &mut measured);
    let available = (width - padding * 2 - icon_width).max(0);
    let content_width = measured.cx.min(available).max(0) + icon_width;
    let start = if sidebar_button { rect.left + padding }
        else { rect.left + (width - content_width) / 2 } + shift;
    if let Some(kind) = kind { icon(hdc, kind, start, icon_y, dpi, foreground); }
    draw_text(hdc, RECT {
        left: start + icon_width,
        top: rect.top + shift,
        right: rect.right - padding + shift,
        bottom: rect.bottom + shift,
    }, &wide, foreground, DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
}

pub(super) unsafe fn row(
    item: &DRAWITEMSTRUCT,
    p: Palette,
    dpi: u32,
    font: HFONT,
    name: &str,
    remote: &str,
    point: &str,
    state: RunState,
) {
    let hdc = item.hDC;
    let Some(_dc) = SavedDc::new(hdc) else { return; };
    let rect = item.rcItem;
    if rect.right <= rect.left || rect.bottom <= rect.top { return; }
    IntersectClipRect(hdc, rect.left, rect.top, rect.right, rect.bottom);
    if !font.is_null() { SelectObject(hdc, font); }
    SetBkMode(hdc, TRANSPARENT as i32);
    let selected = item.itemState & ODS_SELECTED != 0;
    let hot = item.itemState & ODS_HOTLIGHT != 0;
    let disabled = item.itemState & (ODS_DISABLED | ODS_GRAYED) != 0;
    let focused = item.itemState & (ODS_FOCUS | ODS_NOFOCUSRECT) == ODS_FOCUS;
    fill(hdc, rect, p.panel);
    let face = inset(rect, px(4, dpi));
    let face_color = if selected { p.selected } else if hot { p.hover } else { p.panel };
    rounded(hdc, face, px(7, dpi), Some(face_color), if focused { Some(p.accent) } else { None });
    let center_y = rect.top + (rect.bottom - rect.top) / 2;
    let icon_rect = RECT {
        left: rect.left + px(16, dpi),
        top: center_y - px(18, dpi),
        right: rect.left + px(52, dpi),
        bottom: center_y + px(18, dpi),
    };
    rounded(hdc, icon_rect, px(7, dpi), Some(if selected { mix(p.selected, p.accent, 8) } else { p.input }), None);
    icon(hdc, Icon::Drive, icon_rect.left + px(8, dpi), icon_rect.top + px(8, dpi), dpi,
        if disabled { p.disabled } else if selected { p.accent } else { p.secondary });
    let status_color = if disabled { p.disabled } else {
        match state {
            RunState::Ready => p.success,
            RunState::Starting | RunState::Stopping => p.warning,
            RunState::Failed => p.error,
            RunState::Stopped => p.muted,
        }
    };
    let badge = RECT {
        left: rect.right - px(104, dpi),
        right: rect.right - px(16, dpi),
        top: center_y - px(13, dpi),
        bottom: center_y + px(13, dpi),
    };
    let text_left = icon_rect.right + px(12, dpi);
    let text_right = badge.left - px(12, dpi);
    let mut name_wide = Utf16::new();
    name_wide.append(name);
    draw_text(hdc, RECT {
        left: text_left, right: text_right,
        top: center_y - px(24, dpi), bottom: center_y - px(2, dpi),
    }, &name_wide, if disabled { p.disabled } else { p.text }, DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
    let mut location = Utf16::new();
    location.append(remote);
    if !remote.is_empty() && !point.is_empty() { location.append("  ·  "); }
    location.append(point);
    draw_text(hdc, RECT {
        left: text_left, right: text_right,
        top: center_y + px(2, dpi), bottom: center_y + px(24, dpi),
    }, &location, if disabled { p.disabled } else { p.muted }, DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
    if badge.left > text_left {
        rounded(hdc, badge, px(6, dpi), Some(mix(face_color, status_color, 10)), None);
        SelectObject(hdc, GetStockObject(DC_BRUSH));
        SelectObject(hdc, GetStockObject(NULL_PEN));
        SetDCBrushColor(hdc, status_color);
        let dot_left = badge.left + px(10, dpi);
        let dot_top = center_y - px(3, dpi);
        Ellipse(hdc, dot_left, dot_top, dot_left + px(6, dpi), dot_top + px(6, dpi));
        let mut label = Utf16::new();
        label.append(state.label());
        draw_text(hdc, RECT {
            left: badge.left + px(23, dpi), right: badge.right - px(8, dpi), ..badge
        }, &label, status_color, DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX);
    }
}
