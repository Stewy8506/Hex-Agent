use std::time::Duration;

use super::presets::SpringPreset;

pub const SETTLE_POSITION_EPSILON: f32 = 1e-3;
pub const SETTLE_VELOCITY_EPSILON: f32 = 1e-2;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Spring {
    pub pos: f32,
    pub vel: f32,
    pub target: f32,
    pub preset: SpringPreset,
    pub settled: bool,
}

impl Spring {
    pub fn new(pos: f32, preset: SpringPreset) -> Self {
        Self {
            pos,
            vel: 0.0,
            target: pos,
            preset,
            settled: true,
        }
    }

    pub const fn value(self) -> f32 {
        self.pos
    }

    pub const fn velocity(self) -> f32 {
        self.vel
    }

    pub const fn target(self) -> f32 {
        self.target
    }

    pub const fn preset(self) -> SpringPreset {
        self.preset
    }

    pub const fn is_settled(self) -> bool {
        self.settled
    }

    pub fn retarget(&mut self, target: f32, preset: SpringPreset) -> bool {
        let target_changed = target != self.target;
        let preset_changed = preset != self.preset;
        self.target = target;
        self.preset = preset;
        if target_changed {
            self.settled = false;
        }
        target_changed || (!self.settled && preset_changed)
    }

    pub fn handoff(&mut self, velocity: f32) {
        self.vel = velocity;
        self.settled = false;
    }

    pub fn snap(&mut self, target: f32) {
        self.pos = target;
        self.vel = 0.0;
        self.target = target;
        self.settled = true;
    }

    pub fn step(&mut self, dt: Duration) -> bool {
        if self.settled || dt.is_zero() {
            return false;
        }

        let dt = dt.as_secs_f32().min(1.0 / 30.0);
        let acceleration =
            -self.preset.stiffness() * (self.pos - self.target) - self.preset.damping() * self.vel;
        self.vel += acceleration * dt;
        self.pos += self.vel * dt;

        if (self.pos - self.target).abs() <= SETTLE_POSITION_EPSILON
            && self.vel.abs() <= SETTLE_VELOCITY_EPSILON
        {
            self.snap(self.target);
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::motion::{BOUNCY, SMOOTH};
    use core::prelude::v1::test;

    #[test]
    fn retarget_preserves_position_and_velocity() {
        let mut spring = Spring::new(0.0, SMOOTH);
        spring.retarget(1.0, SMOOTH);
        spring.step(Duration::from_millis(32));
        let value = spring.value();
        let velocity = spring.velocity();
        assert!(velocity > 0.0);

        assert!(spring.retarget(0.0, BOUNCY));
        assert_eq!(spring.value(), value);
        assert_eq!(spring.velocity(), velocity);
        assert_eq!(spring.target(), 0.0);
        assert_eq!(spring.preset(), BOUNCY);
    }

    #[test]
    fn velocity_handoff_carries_momentum_through_target() {
        let mut spring = Spring::new(0.0, BOUNCY);
        spring.retarget(1.0, BOUNCY);
        spring.step(Duration::from_millis(16));
        spring.handoff(2.0);
        assert_eq!(spring.velocity(), 2.0);
        assert!(!spring.is_settled());

        let mut passed_target = false;
        for _ in 0..600 {
            if spring.is_settled() {
                break;
            }
            spring.step(Duration::from_millis(16));
            passed_target |= spring.pos > spring.target;
        }
        assert!(passed_target);
        assert!(spring.is_settled());
        assert_eq!(spring.pos, spring.target);
        assert_eq!(spring.vel, 0.0);
    }

    #[test]
    fn large_dt_is_clamped_and_stable() {
        let mut clamped = Spring::new(0.0, BOUNCY);
        let mut huge = Spring::new(0.0, BOUNCY);
        clamped.retarget(1.0, BOUNCY);
        huge.retarget(1.0, BOUNCY);
        clamped.step(Duration::from_secs_f32(1.0 / 30.0));
        huge.step(Duration::from_secs(10));
        assert_eq!(clamped.value(), huge.value());
        assert_eq!(clamped.velocity(), huge.velocity());

        for _ in 0..1000 {
            huge.step(Duration::from_secs(10));
            assert!(huge.value().is_finite());
            assert!(huge.velocity().is_finite());
        }
        assert!(huge.is_settled());
        assert_eq!(huge.value(), 1.0);
        assert_eq!(huge.velocity(), 0.0);
    }

    #[test]
    fn settle_snaps_and_stops_stepping() {
        let mut spring = Spring::new(0.0, SMOOTH);
        spring.retarget(1.0, SMOOTH);
        for _ in 0..600 {
            if spring.is_settled() {
                break;
            }
            spring.step(Duration::from_millis(16));
        }
        assert!(spring.is_settled());
        assert_eq!(spring.value(), 1.0);
        assert_eq!(spring.velocity(), 0.0);
        assert!(!spring.step(Duration::from_millis(16)));
    }
}
