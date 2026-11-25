//! Platform window interactions that GPUI does not expose uniformly.

use gpui::{
    div, App, Div, InteractiveElement, MouseButton, MouseDownEvent, Styled, Window,
    WindowControlArea,
};

/// Empty header strip that moves the window like a native titlebar.
pub fn titlebar_drag_region(debug_name: &'static str) -> Div {
    div()
        .h_full()
        .debug_selector(move || debug_name.to_string())
        .window_control_area(WindowControlArea::Drag)
        .on_mouse_down(MouseButton::Left, on_titlebar_mouse_down)
}

/// Only macOS needs the app to drive this. Elsewhere the region reports a
/// caption hit-test, so the click must stay unhandled for the OS to run its
/// own move and maximize loops.
pub fn on_titlebar_mouse_down(event: &MouseDownEvent, window: &mut Window, cx: &mut App) {
    #[cfg(target_os = "macos")]
    {
        if event.click_count == 2 {
            window.titlebar_double_click();
        } else {
            begin_window_drag();
        }
        cx.stop_propagation();
    }

    #[cfg(not(target_os = "macos"))]
    {
        let _ = (event, window, cx);
    }
}

#[cfg(target_os = "macos")]
fn begin_window_drag() {
    use objc2::MainThreadMarker;
    use objc2_app_kit::NSApplication;

    let Some(mtm) = MainThreadMarker::new() else {
        return;
    };
    let Some(event) = NSApplication::sharedApplication(mtm).currentEvent() else {
        return;
    };
    let Some(ns_window) = event.window(mtm) else {
        return;
    };
    ns_window.performWindowDragWithEvent(&event);
}
