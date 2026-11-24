//! Tool call cards in the mockup token language (parity plan P5, derived
//! surfaces — tool cards are not in DesktopSession.dc.html, so they are styled
//! from the same token language: `#171717` surface, `#222` border, radius 14,
//! mono input/output blocks on `#101010`).
//!
//! Status indicators: running turns use the mockup's `.sp` braille spinner,
//! success paints the derived `check.svg` in sage, failure paints `x.svg` in
//! rose — never the ⏳✓✗ glyphs (D21).

use std::rc::Rc;

use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::diff_view::DiffView;
use crate::ui::icon_svg;
use crate::ui::motion::{self, RevealMotion, RevealValues};

pub type ToolFoldClickHandler = Rc<dyn Fn(&str, &ClickEvent, &mut Window, &mut App)>;

/// Tool card corner radius (derived token language, plan P5).
pub const TOOL_CARD_RADIUS: f32 = 14.0;
/// Tool card body text size (mono blocks share the diff scale).
pub const TOOL_BODY_SIZE: f32 = 12.5;
pub const TOOL_BODY_LINE_HEIGHT: f32 = 21.0;
/// Tool header label size.
pub const TOOL_NAME_SIZE: f32 = 12.5;
/// Fold label / duration size (mockup `.chip` scale).
pub const TOOL_META_SIZE: f32 = 11.5;
/// Inner mono-block horizontal padding.
pub const TOOL_BLOCK_PADDING_X: f32 = 16.0;
pub const TOOL_BLOCK_PADDING_Y: f32 = 10.0;

/// Execution status of a tool invocation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolStatus {
    /// In flight / executing (braille spinner, accent).
    Running,
    /// Executed successfully with exit code 0 (sage check).
    Success,
    /// Failed or returned non-zero error (rose cross).
    Failed,
}

impl ToolStatus {
    /// The tinted SVG status icon: sage `check` on success, rose `x` on
    /// failure; running shows the braille spinner instead (no icon).
    pub fn icon_name(&self) -> Option<&'static str> {
        match self {
            ToolStatus::Running => None,
            ToolStatus::Success => Some("check"),
            ToolStatus::Failed => Some("x"),
        }
    }

    pub fn color(&self, theme: &HermesTheme) -> Rgba {
        match self {
            ToolStatus::Running => theme.accent_blue(),
            ToolStatus::Success => theme.sage_fg(),
            ToolStatus::Failed => theme.rose_fg(),
        }
    }
}

/// Returns a descriptive icon for a recognized tool name.
pub fn tool_icon(name: &str) -> &'static str {
    match name {
        "run_command" | "execute_command" | "bash" | "shell" => "bolt",
        "write_file" | "edit_file" | "patch" => "pencil",
        "read_file" | "list_directory" | "find_files" => "notebooks",
        "web_search" | "browser" => "search",
        "delegate" | "subagent" => "team",
        _ => "settings",
    }
}

/// Tool call card component.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolCallCard {
    pub call_id: String,
    pub tool_name: String,
    pub arguments_json: String,
    pub output_text: Option<String>,
    pub diff: Option<DiffView>,
    pub status: ToolStatus,
    pub duration_ms: Option<u64>,
    pub input_expanded: bool,
    pub output_expanded: bool,
}

impl ToolCallCard {
    pub fn new(
        call_id: impl Into<String>,
        tool_name: impl Into<String>,
        arguments_json: impl Into<String>,
    ) -> Self {
        Self {
            call_id: call_id.into(),
            tool_name: tool_name.into(),
            arguments_json: arguments_json.into(),
            output_text: None,
            diff: None,
            status: ToolStatus::Running,
            duration_ms: None,
            input_expanded: false,
            output_expanded: true,
        }
    }

    pub fn with_output(mut self, output: impl Into<String>) -> Self {
        self.output_text = Some(output.into());
        self.status = ToolStatus::Success;
        self
    }

    pub fn with_error(mut self, error: impl Into<String>) -> Self {
        self.output_text = Some(error.into());
        self.status = ToolStatus::Failed;
        self
    }

    pub fn with_diff(mut self, diff: DiffView) -> Self {
        self.diff = Some(diff);
        self
    }

    pub fn with_duration(mut self, duration_ms: u64) -> Self {
        self.duration_ms = Some(duration_ms);
        self
    }

    pub fn toggle_input(&mut self) {
        self.input_expanded = !self.input_expanded;
    }

    pub fn toggle_output(&mut self) {
        self.output_expanded = !self.output_expanded;
    }

    /// The status indicator element: braille spinner for running turns, or
    /// the tinted status SVG.
    fn render_status(&self, theme: &HermesTheme) -> AnyElement {
        match self.status {
            ToolStatus::Running => {
                let spinner = div()
                    .font_family(theme.font_mono()[0].clone())
                    .text_size(px(MockupTypeScale::SPINNER_SIZE))
                    .text_color(theme.accent_blue());
                if motion::reduced_motion() {
                    spinner
                        .child(motion::spinner_frame(motion::SPIN_PERIOD).to_string())
                        .into_any_element()
                } else {
                    let id = SharedString::from(format!("tool_spin_{}", self.call_id));
                    spinner
                        .with_animation(
                            id,
                            Animation::new(motion::SPIN_PERIOD).repeat(),
                            |el, phase| {
                                let elapsed = motion::SPIN_PERIOD.mul_f32(phase);
                                el.child(motion::spinner_frame(elapsed).to_string())
                            },
                        )
                        .into_any_element()
                }
            }
            ToolStatus::Success => div()
                .child(icon_svg("check", 14.0).text_color(theme.sage_fg()))
                .into_any_element(),
            ToolStatus::Failed => div()
                .child(icon_svg("x", 14.0).text_color(theme.rose_fg()))
                .into_any_element(),
        }
    }

    /// Renders the tool header row.
    fn render_header(&self, theme: &HermesTheme) -> Div {
        let icon = tool_icon(&self.tool_name);
        let duration_str = self
            .duration_ms
            .map(|ms| format!("{}ms", ms))
            .unwrap_or_default();

        div()
            .w_full()
            .flex()
            .flex_row()
            .items_center()
            .justify_between()
            .px(px(TOOL_BLOCK_PADDING_X))
            .py(px(8.0))
            .child(
                div()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap_2()
                    .child(icon_svg(icon, 14.0).text_color(theme.mockup_text_muted))
                    .child(
                        div()
                            .font_family(theme.font_mono()[0].clone())
                            .text_size(px(TOOL_NAME_SIZE))
                            .font_weight(FontWeight::MEDIUM)
                            .text_color(theme.mockup_text)
                            .child(self.tool_name.clone()),
                    ),
            )
            .child(
                div()
                    .flex()
                    .flex_row()
                    .items_center()
                    .gap(px(8.0))
                    .child(
                        div()
                            .text_size(px(TOOL_META_SIZE))
                            .text_color(theme.mockup_text_dim)
                            .child(duration_str),
                    )
                    .child(self.render_status(theme)),
            )
    }

    /// Renders one fold header (caret chevron + label).
    fn render_fold_header(
        &self,
        theme: &HermesTheme,
        fold_id: &str,
        label: &str,
        expanded: bool,
        on_click: Option<&ToolFoldClickHandler>,
        reveal: Option<(RevealMotion, &App)>,
    ) -> AnyElement {
        let progress = reveal
            .map(|(reveal, cx)| reveal.values(cx).spatial)
            .unwrap_or(f32::from(expanded));
        let caret = icon_svg("chevron-right", 12.0)
            .text_color(theme.mockup_text_dim)
            .with_transformation(Transformation::rotate(radians(
                progress.clamp(0.0, 1.0) * std::f32::consts::FRAC_PI_2,
            )));
        let header = div()
            .flex()
            .flex_row()
            .items_center()
            .gap_1()
            .px(px(TOOL_BLOCK_PADDING_X))
            .py(px(6.0))
            .child(caret)
            .child(
                div()
                    .text_size(px(TOOL_META_SIZE))
                    .text_color(theme.mockup_text_dim)
                    .child(label.to_string()),
            );
        if let Some(on_click) = on_click {
            let call_id = self.call_id.clone();
            let on_click = on_click.clone();
            let selector = format!("tool_{fold_id}_{call_id}");
            header
                .id(SharedString::from(format!("tool_{fold_id}_{}", call_id)))
                .debug_selector(move || selector)
                .cursor_pointer()
                .hover(|s| s.bg(theme.mockup_hover))
                .on_click(move |event, window, cx| on_click(&call_id, event, window, cx))
                .into_any_element()
        } else {
            header.into_any_element()
        }
    }

    /// Renders the tool input / parameters fold.
    fn render_input_fold(
        &self,
        theme: &HermesTheme,
        on_click: Option<&ToolFoldClickHandler>,
        reveal: Option<(RevealMotion, &App)>,
    ) -> Div {
        let mut section = div().w_full().flex().flex_col();
        section = section.child(self.render_fold_header(
            theme,
            "input",
            "Parameters",
            self.input_expanded,
            on_click,
            reveal,
        ));

        let values = reveal.map(|(reveal, cx)| reveal.values(cx));
        if values.map_or(self.input_expanded, RevealValues::is_visible) {
            let mut body = self.render_mono_block(theme, &self.arguments_json, false);
            if let Some(values) = values {
                body = values.apply(body);
            }
            section = section.child(body);
        }

        section
    }

    /// Renders the output / observation fold.
    fn render_output_fold(
        &self,
        theme: &HermesTheme,
        on_click: Option<&ToolFoldClickHandler>,
        reveal: Option<(RevealMotion, &App)>,
    ) -> Option<Div> {
        let output = self.output_text.as_ref()?;
        let mut section = div().w_full().flex().flex_col();
        section = section.child(self.render_fold_header(
            theme,
            "output",
            "Output",
            self.output_expanded,
            on_click,
            reveal,
        ));

        let values = reveal.map(|(reveal, cx)| reveal.values(cx));
        if values.map_or(self.output_expanded, RevealValues::is_visible) {
            let mut body = self.render_mono_block(theme, output, true);
            if let Some(values) = values {
                body = values.apply(body);
            }
            section = section.child(body);
        }

        Some(section)
    }

    /// A mono `#101010` block: 12.5/21 Geist Mono, whitespace preserved;
    /// failure output tints rose.
    fn render_mono_block(&self, theme: &HermesTheme, text: &str, is_output: bool) -> Div {
        div()
            .w_full()
            .font_family(theme.font_mono()[0].clone())
            .text_size(px(TOOL_BODY_SIZE))
            .line_height(px(TOOL_BODY_LINE_HEIGHT))
            .px(px(TOOL_BLOCK_PADDING_X))
            .py(px(TOOL_BLOCK_PADDING_Y))
            .bg(theme.mockup_sidebar)
            .whitespace_nowrap()
            .text_color(if is_output && self.status == ToolStatus::Failed {
                theme.rose_fg()
            } else {
                theme.mockup_text_muted
            })
            .child(text.to_string())
    }

    /// The diff view hosted by this card, when the tool call produced a diff.
    pub fn diff_view(&self) -> Option<&DiffView> {
        self.diff.as_ref()
    }

    pub fn render(&self, theme: &HermesTheme) -> Div {
        self.render_with_fold_handlers(theme, None, None)
    }

    pub fn render_with_fold_handlers(
        &self,
        theme: &HermesTheme,
        on_input: Option<ToolFoldClickHandler>,
        on_output: Option<ToolFoldClickHandler>,
    ) -> Div {
        self.render_inner(theme, on_input, on_output, None, None)
    }

    pub fn render_with_fold_handlers_motion(
        &self,
        theme: &HermesTheme,
        on_input: Option<ToolFoldClickHandler>,
        on_output: Option<ToolFoldClickHandler>,
        window: &mut Window,
        cx: &mut App,
        owner: EntityId,
    ) -> Div {
        let input =
            RevealMotion::for_identity(owner, "tool_reveal", &format!("{}:input", self.call_id));
        let output =
            RevealMotion::for_identity(owner, "tool_reveal", &format!("{}:output", self.call_id));
        input.ensure(window, cx, f32::from(self.input_expanded));
        output.ensure(window, cx, f32::from(self.output_expanded));
        self.render_inner(
            theme,
            on_input,
            on_output,
            Some((input, cx)),
            Some((output, cx)),
        )
    }

    fn render_inner(
        &self,
        theme: &HermesTheme,
        on_input: Option<ToolFoldClickHandler>,
        on_output: Option<ToolFoldClickHandler>,
        input_reveal: Option<(RevealMotion, &App)>,
        output_reveal: Option<(RevealMotion, &App)>,
    ) -> Div {
        let mut card = div()
            .w_full()
            .flex()
            .flex_col()
            .rounded(px(TOOL_CARD_RADIUS))
            .border_1()
            .border_color(theme.mockup_border)
            .bg(theme.mockup_surface)
            .overflow_hidden()
            .child(self.render_header(theme))
            .child(self.render_input_fold(theme, on_input.as_ref(), input_reveal));

        if let Some(diff_view) = &self.diff {
            card = card.child(
                div()
                    .w_full()
                    .border_t_1()
                    .border_color(theme.mockup_border)
                    .child(diff_view.render(theme)),
            );
        }

        if let Some(output_fold) = self.render_output_fold(theme, on_output.as_ref(), output_reveal)
        {
            card = card.child(output_fold);
        }

        card
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_tool_icons_and_status() {
        let theme = HermesTheme::default();

        assert_eq!(tool_icon("run_command"), "bolt");
        assert_eq!(tool_icon("write_file"), "pencil");
        assert_eq!(tool_icon("read_file"), "notebooks");
        assert_eq!(tool_icon("web_search"), "search");
        assert_eq!(tool_icon("unknown_tool"), "settings");

        // D21: status glyphs are SVG icon names / spinner, never ⏳✓✗.
        assert_eq!(ToolStatus::Running.icon_name(), None);
        assert_eq!(ToolStatus::Success.icon_name(), Some("check"));
        assert_eq!(ToolStatus::Failed.icon_name(), Some("x"));
        assert_eq!(ToolStatus::Success.color(&theme), theme.sage_fg());
        assert_eq!(ToolStatus::Failed.color(&theme), theme.rose_fg());
        assert_eq!(ToolStatus::Running.color(&theme), theme.accent_blue());
    }

    #[test]
    fn test_tool_card_builder_and_toggles() {
        let mut card = ToolCallCard::new("tc_1", "run_command", r#"{"cmd":"cargo test"}"#)
            .with_output("test result: ok. 14 passed")
            .with_duration(142);

        assert_eq!(card.tool_name, "run_command");
        assert_eq!(card.status, ToolStatus::Success);
        assert_eq!(card.duration_ms, Some(142));
        assert!(!card.input_expanded);
        assert!(card.output_expanded);

        card.toggle_input();
        assert!(card.input_expanded);

        card.toggle_output();
        assert!(!card.output_expanded);
    }
}
