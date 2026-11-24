use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};

use gpui::{px, App, EntityId, Hsla, InteractiveElement, MouseButton, Styled, Window};

use super::lerp::lerp_hsla;
use super::system::{MotionSystem, SpringKey};
use super::SMOOTH;

pub const REVEAL_RISE: f32 = 8.0;
pub const CONTROL_PRESS_OFFSET: f32 = 1.0;
pub const CONTROL_PRESS_OPACITY: f32 = 0.18;

pub fn slot(identity: &str) -> u64 {
    let mut hasher = DefaultHasher::new();
    identity.hash(&mut hasher);
    hasher.finish()
}

#[derive(Clone, Copy, Debug)]
pub struct RevealMotion {
    pub spatial: SpringKey,
    pub opacity: SpringKey,
}

impl RevealMotion {
    pub fn for_identity(owner: EntityId, namespace: &'static str, identity: &str) -> Self {
        let slot = slot(&format!("{namespace}:{identity}"));
        Self {
            spatial: SpringKey::for_slot(owner, "reveal_spatial", slot),
            opacity: SpringKey::for_slot(owner, "reveal_opacity", slot),
        }
    }

    pub fn ensure(&self, window: &mut Window, cx: &mut App, target: f32) {
        if !MotionSystem::has(cx, self.spatial) {
            MotionSystem::snap(window, cx, self.spatial, target, SMOOTH);
        }
        if !MotionSystem::has(cx, self.opacity) {
            MotionSystem::snap(window, cx, self.opacity, target, SMOOTH);
        }
    }

    pub fn enter(&self, window: &mut Window, cx: &mut App) {
        if !MotionSystem::has(cx, self.spatial) || !MotionSystem::has(cx, self.opacity) {
            MotionSystem::snap(window, cx, self.spatial, 0.0, SMOOTH);
            MotionSystem::snap(window, cx, self.opacity, 0.0, SMOOTH);
        }
        MotionSystem::set_target(window, cx, self.spatial, 1.0, SMOOTH);
        MotionSystem::set_opacity_target(window, cx, self.opacity, 1.0, SMOOTH);
    }

    pub fn exit(&self, window: &mut Window, cx: &mut App) {
        if !MotionSystem::has(cx, self.spatial) || !MotionSystem::has(cx, self.opacity) {
            MotionSystem::snap(window, cx, self.spatial, 1.0, SMOOTH);
            MotionSystem::snap(window, cx, self.opacity, 1.0, SMOOTH);
        }
        MotionSystem::set_target(window, cx, self.spatial, 0.0, SMOOTH);
        MotionSystem::set_opacity_target(window, cx, self.opacity, 0.0, SMOOTH);
    }

    pub fn set(&self, window: &mut Window, cx: &mut App, expanded: bool) {
        self.ensure(window, cx, f32::from(expanded));
        let target = f32::from(expanded);
        MotionSystem::set_target(window, cx, self.spatial, target, SMOOTH);
        MotionSystem::set_opacity_target(window, cx, self.opacity, target, SMOOTH);
    }

    pub fn values(&self, cx: &App) -> RevealValues {
        RevealValues {
            spatial: MotionSystem::value_or(cx, self.spatial, 0.0),
            opacity: MotionSystem::value_or(cx, self.opacity, 0.0),
        }
    }

    pub fn remove(&self, cx: &mut App) {
        MotionSystem::remove(cx, self.spatial);
        MotionSystem::remove(cx, self.opacity);
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RevealValues {
    pub spatial: f32,
    pub opacity: f32,
}

impl RevealValues {
    pub fn is_visible(self) -> bool {
        self.spatial.abs() > 1e-4 || self.opacity.abs() > 1e-4
    }

    pub fn apply<T: Styled>(self, element: T) -> T {
        let spatial = self.spatial.clamp(0.0, 1.0);
        element
            .opacity(self.opacity.clamp(0.0, 1.0))
            .mt(px(REVEAL_RISE * (1.0 - spatial)))
    }
}

#[derive(Clone, Copy, Debug)]
pub struct ControlMotion {
    pub press: SpringKey,
    pub hover: SpringKey,
}

impl ControlMotion {
    pub fn for_identity(owner: EntityId, namespace: &'static str, identity: &str) -> Self {
        let slot = slot(&format!("{namespace}:{identity}"));
        Self {
            press: SpringKey::for_slot(owner, "control_press", slot),
            hover: SpringKey::for_slot(owner, "control_hover", slot),
        }
    }

    pub fn set_pressed(&self, window: &mut Window, cx: &mut App, pressed: bool) {
        MotionSystem::set_target(window, cx, self.press, f32::from(pressed), super::SNAPPY);
    }

    pub fn set_hovered(&self, window: &mut Window, cx: &mut App, hovered: bool) {
        MotionSystem::set_target(window, cx, self.hover, f32::from(hovered), super::SNAPPY);
    }

    pub fn values(&self, cx: &App) -> ControlValues {
        ControlValues {
            press: MotionSystem::value_or(cx, self.press, 0.0),
            hover: MotionSystem::value_or(cx, self.hover, 0.0),
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct ControlValues {
    pub press: f32,
    pub hover: f32,
}

impl ControlValues {
    pub fn apply_press<T: Styled>(self, element: T) -> T {
        let press = self.press.clamp(0.0, 1.0);
        element
            .mt(px(CONTROL_PRESS_OFFSET * press))
            .opacity(1.0 - CONTROL_PRESS_OPACITY * press)
    }

    pub fn color(self, base: Hsla, hover: Hsla) -> Hsla {
        lerp_hsla(base, hover, self.hover.clamp(0.0, 1.0))
    }
}

pub fn press_feedback<T: InteractiveElement + Styled>(
    button: T,
    control: ControlMotion,
    cx: &App,
) -> T {
    control
        .values(cx)
        .apply_press(button)
        .on_mouse_down(MouseButton::Left, move |_, window, cx| {
            control.set_pressed(window, cx, true);
        })
        .on_mouse_up(MouseButton::Left, move |_, window, cx| {
            control.set_pressed(window, cx, false);
        })
        .on_mouse_up_out(MouseButton::Left, move |_, window, cx| {
            control.set_pressed(window, cx, false);
        })
}

pub fn hover_feedback<T: gpui::StatefulInteractiveElement>(button: T, control: ControlMotion) -> T {
    button.on_hover(move |hovered, window, cx| {
        control.set_hovered(window, cx, *hovered);
    })
}

#[cfg(test)]
mod tests {
    use super::super::reduced::MotionPolicy;
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn dynamic_slots_are_stable_and_distinct() {
        assert_eq!(slot("call_1:input"), slot("call_1:input"));
        assert_ne!(slot("call_1:input"), slot("call_1:output"));
        assert_ne!(slot("/tmp/a.txt"), slot("/tmp/b.txt"));
    }

    #[gpui::test]
    fn reduced_reveal_snaps_space_and_fades_opacity(cx: &mut gpui::TestAppContext) {
        cx.set_global(MotionSystem::with_policy(MotionPolicy::new(true)));
        let (entity, vc) = cx.add_window_view(|_, _| gpui::Empty);
        let owner = entity.entity_id();
        let reveal = RevealMotion::for_identity(owner, "test_reveal", "row");
        let control = ControlMotion::for_identity(owner, "test_control", "send");

        vc.update(|window, app| {
            let window_id = window.window_handle().window_id();
            reveal.ensure(window, app, 0.0);
            assert!(MotionSystem::has(app, reveal.spatial));
            assert!(MotionSystem::policy(app).is_reduced());
            assert!(!app.global::<MotionSystem>().driver_active(window_id));

            reveal.enter(window, app);
            assert_eq!(reveal.values(app).spatial, 1.0);
            assert_eq!(reveal.values(app).opacity, 0.0);
            assert!(app.global::<MotionSystem>().driver_active(window_id));
            for _ in 0..600 {
                app.global_mut::<MotionSystem>()
                    .tick(window_id, std::time::Duration::from_millis(16));
            }
            assert_eq!(reveal.values(app).opacity, 1.0);
            assert!(!app.global::<MotionSystem>().driver_active(window_id));

            control.set_pressed(window, app, true);
            assert_eq!(control.values(app).press, 1.0);
            assert!(!app.global::<MotionSystem>().driver_active(window_id));

            reveal.exit(window, app);
            assert_eq!(reveal.values(app).spatial, 0.0);
            assert_eq!(reveal.values(app).opacity, 1.0);
            for _ in 0..600 {
                app.global_mut::<MotionSystem>()
                    .tick(window_id, std::time::Duration::from_millis(16));
            }
            assert_eq!(reveal.values(app).opacity, 0.0);
            assert!(!app.global::<MotionSystem>().driver_active(window_id));
        });
    }
}
