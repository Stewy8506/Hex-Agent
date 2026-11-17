//! Native macOS Metal-accelerated window configuration for HEX Desktop.

use gpui::*;

/// Window dimensions and macOS Metal acceleration configuration.
#[derive(Debug, Clone)]
pub struct WindowConfig {
    /// Window width in pixels.
    pub width: Pixels,
    /// Window height in pixels.
    pub height: Pixels,
    /// Minimum allowed window width.
    pub min_width: Pixels,
    /// Minimum allowed window height.
    pub min_height: Pixels,
    /// Traffic light horizontal offset.
    pub traffic_light_x: Pixels,
    /// Traffic light vertical offset.
    pub traffic_light_y: Pixels,
    /// Window title.
    pub title: Option<String>,
    /// Target frame rate (e.g. 120 FPS on ProMotion Apple Silicon displays).
    pub target_fps: u32,
}

impl Default for WindowConfig {
    fn default() -> Self {
        Self {
            width: px(1200.0),
            height: px(800.0),
            min_width: px(900.0),
            min_height: px(600.0),
            traffic_light_x: px(14.0),
            traffic_light_y: px(16.0),
            title: Some("HEX Autonomous AI Workbench".to_string()),
            target_fps: 120,
        }
    }
}

impl WindowConfig {
    /// Returns default centered window bounds (calculated for a standard 1920x1080 display).
    pub fn default_bounds(&self) -> Bounds<Pixels> {
        let default_screen_w = px(1920.0);
        let default_screen_h = px(1080.0);
        let origin_x = (default_screen_w - self.width) / 2.0;
        let origin_y = (default_screen_h - self.height) / 2.0;
        Bounds {
            origin: point(origin_x.max(px(0.0)), origin_y.max(px(0.0))),
            size: size(self.width, self.height),
        }
    }

    /// Calculates centered bounds on a specific display bounds rectangle.
    pub fn centered_on_display(&self, display: Bounds<Pixels>) -> Bounds<Pixels> {
        let origin_x = display.origin.x + (display.size.width - self.width) / 2.0;
        let origin_y = display.origin.y + (display.size.height - self.height) / 2.0;
        Bounds {
            origin: point(origin_x.max(display.origin.x), origin_y.max(display.origin.y)),
            size: size(self.width, self.height),
        }
    }

    /// Titlebar options with translucent background and custom traffic light placement.
    pub fn titlebar_options(&self) -> TitlebarOptions {
        TitlebarOptions {
            title: self.title.clone().map(Into::into),
            appears_transparent: true,
            traffic_light_position: Some(point(self.traffic_light_x, self.traffic_light_y)),
        }
    }

    /// Construct `WindowOptions` targeting native macOS Metal rendering at 120 FPS
    /// with blurred vibrancy background.
    pub fn window_options(&self, bounds: Option<Bounds<Pixels>>) -> WindowOptions {
        let b = bounds.unwrap_or_else(|| self.default_bounds());
        WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(b)),
            titlebar: Some(self.titlebar_options()),
            window_background: WindowBackgroundAppearance::Blurred,
            window_min_size: Some(size(self.min_width, self.min_height)),
            focus: true,
            show: true,
            ..Default::default()
        }
    }
}

/// View states available in the desktop cockpit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DesktopView {
    Start,
    Session,
    Settings,
}

/// Root GPUI view holding workspace context and view dispatch state.
pub struct DesktopWindow {
    pub workspace_path: Option<String>,
    pub status_message: String,
    pub active_view: DesktopView,
    pub shell: crate::ui::AppShell,
}

impl DesktopWindow {
    pub fn new() -> Self {
        Self {
            workspace_path: None,
            status_message: "Ready".to_string(),
            active_view: DesktopView::Start,
            shell: crate::ui::AppShell::new(),
        }
    }

    pub fn with_workspace(workspace_path: impl Into<String>) -> Self {
        let path = workspace_path.into();
        Self {
            workspace_path: Some(path.clone()),
            status_message: "Workspace loaded".to_string(),
            active_view: DesktopView::Start,
            shell: crate::ui::AppShell::with_workspace(path, "experimental-features"),
        }
    }

    pub fn set_view(&mut self, view: DesktopView) {
        self.active_view = view;
    }
}

impl Default for DesktopWindow {
    fn default() -> Self {
        Self::new()
    }
}

impl Render for DesktopWindow {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let theme = crate::theme::HermesTheme::get(cx);
        match self.active_view {
            DesktopView::Session => self.shell.render_shell(theme),
            _ => div()
                .size_full()
                .bg(rgb(0x09090b))
                .text_color(rgb(0xfafafa))
                .flex()
                .flex_col()
                .child(
                    div()
                        .p_4()
                        .flex()
                        .items_center()
                        .justify_between()
                        .child(
                            div()
                                .text_sm()
                                .font_weight(FontWeight::SEMIBOLD)
                                .child("HEX Autonomous AI Workbench"),
                        )
                        .child(
                            div()
                                .text_xs()
                                .text_color(rgb(0xa1a1aa))
                                .child(self.status_message.clone()),
                        ),
                )
                .child(
                    div()
                        .flex_1()
                        .child(self.shell.render_shell(theme)),
                ),
        }
    }
}
