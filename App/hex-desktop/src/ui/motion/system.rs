use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};

use gpui::{App, EntityId, Global, Window, WindowId};

use super::presets::SpringPreset;
use super::reduced::{reduced_motion, MotionKind, MotionPolicy};
use super::spring::Spring;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct SpringKey {
    pub owner: EntityId,
    pub name: &'static str,
}

struct SpringEntry {
    spring: Spring,
    window_id: WindowId,
}

struct FrameDriver {
    scheduled: bool,
    last_tick: Instant,
}

pub struct MotionSystem {
    springs: HashMap<SpringKey, SpringEntry>,
    drivers: HashMap<WindowId, FrameDriver>,
    policy: MotionPolicy,
}

impl Global for MotionSystem {}

impl Default for MotionSystem {
    fn default() -> Self {
        Self::new()
    }
}

impl MotionSystem {
    pub fn new() -> Self {
        Self {
            springs: HashMap::new(),
            drivers: HashMap::new(),
            policy: MotionPolicy::new(reduced_motion()),
        }
    }

    pub fn value(cx: &App, key: SpringKey) -> f32 {
        cx.try_global::<Self>()
            .and_then(|system| system.springs.get(&key))
            .map_or(1.0, |entry| entry.spring.value())
    }

    pub fn set_target(
        window: &mut Window,
        cx: &mut App,
        key: SpringKey,
        target: f32,
        preset: SpringPreset,
    ) {
        Self::set_target_for_kind(window, cx, key, target, preset, MotionKind::Spatial);
    }

    pub fn set_opacity_target(
        window: &mut Window,
        cx: &mut App,
        key: SpringKey,
        target: f32,
        preset: SpringPreset,
    ) {
        Self::set_target_for_kind(window, cx, key, target, preset, MotionKind::Opacity);
    }

    fn set_target_for_kind(
        window: &mut Window,
        cx: &mut App,
        key: SpringKey,
        target: f32,
        preset: SpringPreset,
        kind: MotionKind,
    ) {
        let window_id = window.window_handle().window_id();
        let changed = cx
            .default_global::<Self>()
            .retarget_for_window(window_id, key, target, preset, kind);
        if !changed {
            return;
        }

        let active = cx.global::<Self>().driver_active(window_id);
        if active {
            cx.global_mut::<Self>().schedule_window(window, window_id);
        }
        cx.notify(key.owner);
    }

    fn retarget_for_window(
        &mut self,
        window_id: WindowId,
        key: SpringKey,
        target: f32,
        preset: SpringPreset,
        kind: MotionKind,
    ) -> bool {
        let current = self
            .springs
            .get(&key)
            .map_or(0.0, |entry| entry.spring.value());
        let preset = self.policy.preset(kind, preset);
        let entry = self.springs.entry(key).or_insert_with(|| SpringEntry {
            spring: Spring::new(current, preset),
            window_id,
        });
        entry.window_id = window_id;
        if self.policy.snaps(kind) {
            let changed = entry.spring.value() != target;
            entry.spring.snap(target);
            changed
        } else {
            entry.spring.retarget(target, preset)
        }
    }

    pub fn driver_active(&self, window_id: WindowId) -> bool {
        self.springs
            .values()
            .any(|entry| entry.window_id == window_id && !entry.spring.is_settled())
    }

    fn schedule_window(&mut self, window: &mut Window, window_id: WindowId) {
        let should_schedule = self
            .drivers
            .get(&window_id)
            .is_none_or(|driver| !driver.scheduled);
        if !should_schedule {
            return;
        }
        self.drivers.insert(
            window_id,
            FrameDriver {
                scheduled: true,
                last_tick: Instant::now(),
            },
        );
        window.on_next_frame(move |window, cx| Self::on_frame(window, cx));
        window.refresh();
    }

    fn on_frame(window: &mut Window, cx: &mut App) {
        let window_id = window.window_handle().window_id();
        let now = Instant::now();
        let (owners, active) = {
            let system = cx.default_global::<Self>();
            let Some(driver) = system.drivers.get_mut(&window_id) else {
                return;
            };
            let dt = now.saturating_duration_since(driver.last_tick);
            driver.last_tick = now;
            let owners = system.tick(window_id, dt);
            let active = system.driver_active(window_id);
            (owners, active)
        };

        if active {
            {
                let system = cx.global_mut::<Self>();
                if let Some(driver) = system.drivers.get_mut(&window_id) {
                    driver.scheduled = false;
                }
            }
            cx.global_mut::<Self>().schedule_window(window, window_id);
        } else {
            cx.global_mut::<Self>().drivers.remove(&window_id);
        }
        for owner in owners {
            cx.notify(owner);
        }
    }

    pub fn tick(&mut self, window_id: WindowId, dt: Duration) -> HashSet<EntityId> {
        let mut owners = HashSet::new();
        for (key, entry) in &mut self.springs {
            if entry.window_id != window_id || entry.spring.is_settled() {
                continue;
            }
            entry.spring.step(dt);
            owners.insert(key.owner);
        }
        owners
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::motion::{SMOOTH, SNAPPY};
    use core::prelude::v1::test;

    fn key(owner: u64, name: &'static str) -> SpringKey {
        SpringKey {
            owner: EntityId::from(owner),
            name,
        }
    }

    #[test]
    fn synthetic_frames_settle_and_stop_notifying() {
        let mut system = MotionSystem::new();
        system.policy = MotionPolicy::new(false);
        let window_id = WindowId::from(1);
        let modal = key(10, "modal");
        assert!(system.retarget_for_window(window_id, modal, 1.0, SMOOTH, MotionKind::Spatial));

        let mut frames = 0;
        while system.driver_active(window_id) && frames < 600 {
            assert!(system
                .tick(window_id, Duration::from_millis(16))
                .contains(&modal.owner));
            frames += 1;
        }
        assert!(!system.driver_active(window_id));
        assert!((system.springs[&modal].spring.value() - 1.0).abs() < 1e-6);
        assert!(system.tick(window_id, Duration::from_millis(16)).is_empty());
    }

    #[test]
    fn retargeting_reuses_the_live_spring() {
        let mut system = MotionSystem::new();
        system.policy = MotionPolicy::new(false);
        let window_id = WindowId::from(1);
        let card = key(10, "card");
        system.retarget_for_window(window_id, card, 1.0, SMOOTH, MotionKind::Spatial);
        system.tick(window_id, Duration::from_millis(32));
        let value = system.springs[&card].spring.value();
        let velocity = system.springs[&card].spring.velocity();

        system.retarget_for_window(window_id, card, 0.0, SNAPPY, MotionKind::Spatial);
        assert_eq!(system.springs[&card].spring.value(), value);
        assert_eq!(system.springs[&card].spring.velocity(), velocity);
        assert_eq!(system.springs[&card].spring.target(), 0.0);
    }

    #[test]
    fn spring_keys_are_namespaced_by_owner_and_name() {
        let mut system = MotionSystem::new();
        system.policy = MotionPolicy::new(false);
        let window_id = WindowId::from(1);
        let first = key(10, "opacity");
        let second = key(10, "position");
        let other_owner = key(11, "opacity");
        for entry in [first, second, other_owner] {
            let target = match entry {
                SpringKey { owner, name } if owner == first.owner && name == first.name => 1.0,
                SpringKey { owner, name } if owner == second.owner && name == second.name => 0.5,
                _ => 0.25,
            };
            system.retarget_for_window(window_id, entry, target, SMOOTH, MotionKind::Spatial);
        }
        system.tick(window_id, Duration::from_millis(16));
        assert_ne!(
            system.springs[&first].spring.value(),
            system.springs[&second].spring.value()
        );
        assert_ne!(
            system.springs[&first].spring.value(),
            system.springs[&other_owner].spring.value()
        );
    }

    #[test]
    fn drivers_only_advance_their_own_window() {
        let mut system = MotionSystem::new();
        system.policy = MotionPolicy::new(false);
        let first_window = WindowId::from(1);
        let second_window = WindowId::from(2);
        let first = key(10, "first");
        let second = key(11, "second");
        system.retarget_for_window(first_window, first, 1.0, SMOOTH, MotionKind::Spatial);
        system.retarget_for_window(second_window, second, 1.0, SMOOTH, MotionKind::Spatial);

        assert!(system
            .tick(first_window, Duration::from_millis(16))
            .contains(&first.owner));
        assert_eq!(system.springs[&second].spring.value(), 0.0);
        assert!(system.driver_active(second_window));
    }

    #[test]
    fn reduced_motion_snaps_space_and_fades_opacity() {
        let mut system = MotionSystem::new();
        system.policy = MotionPolicy::new(true);
        let window_id = WindowId::from(1);
        let position = key(10, "position");
        let opacity = key(10, "opacity");
        system.retarget_for_window(window_id, position, 1.0, SMOOTH, MotionKind::Spatial);
        assert_eq!(system.springs[&position].spring.value(), 1.0);
        assert!(!system.driver_active(window_id));

        system.retarget_for_window(window_id, opacity, 1.0, SMOOTH, MotionKind::Opacity);
        assert!(system.driver_active(window_id));
        assert_eq!(
            system.springs[&opacity].spring.preset().duration,
            super::super::REDUCED_OPACITY_FADE.as_secs_f32()
        );
    }
}
