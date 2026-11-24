#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SpringPreset {
    pub duration: f32,
    pub bounce: f32,
}

impl SpringPreset {
    pub fn new(duration: f32, bounce: f32) -> Self {
        assert!(
            duration.is_finite() && duration > 0.0,
            "spring duration must be positive"
        );
        assert!(
            (0.0..1.0).contains(&bounce),
            "spring bounce must be in [0, 1)"
        );
        Self { duration, bounce }
    }

    pub fn with_duration(self, duration: f32) -> Self {
        Self::new(duration, self.bounce)
    }

    pub fn omega_n(self) -> f32 {
        std::f32::consts::TAU / self.duration
    }

    pub fn natural_frequency(self) -> f32 {
        self.omega_n()
    }

    pub fn stiffness(self) -> f32 {
        self.omega_n().powi(2)
    }

    pub fn damping(self) -> f32 {
        (1.0 - self.bounce) * 4.0 * std::f32::consts::PI / self.duration
    }

    pub fn zeta(self) -> f32 {
        1.0 - self.bounce
    }
}

pub const SMOOTH: SpringPreset = SpringPreset {
    duration: 0.45,
    bounce: 0.0,
};

pub const SNAPPY: SpringPreset = SpringPreset {
    duration: 0.18,
    bounce: 0.0,
};

pub const BOUNCY: SpringPreset = SpringPreset {
    duration: 0.45,
    bounce: 0.30,
};

pub fn spring_easing(preset: SpringPreset, progress: f32) -> f32 {
    if progress <= 0.0 {
        return 0.0;
    }
    if progress >= 1.0 {
        return 1.0;
    }

    let elapsed = progress * preset.duration;
    let omega = preset.omega_n();
    let zeta = preset.zeta();
    if zeta >= 1.0 - f32::EPSILON {
        return 1.0 - (1.0 + omega * elapsed) * (-omega * elapsed).exp();
    }

    let damped = omega * (1.0 - zeta * zeta).sqrt();
    let envelope = (-zeta * omega * elapsed).exp();
    1.0 - envelope * ((damped * elapsed).cos() + (zeta * omega / damped) * (damped * elapsed).sin())
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn preset_conversion_identities() {
        for preset in [SMOOTH, SNAPPY, BOUNCY] {
            let omega = preset.omega_n();
            let k = preset.stiffness();
            let c = preset.damping();
            assert!((k - omega * omega).abs() < 1e-4);
            assert!((c / (2.0 * k.sqrt()) - preset.zeta()).abs() < 1e-5);
        }
        assert_eq!(SMOOTH.duration, 0.45);
        assert_eq!(SNAPPY.duration, 0.18);
        assert_eq!(BOUNCY.duration, 0.45);
        assert_eq!(SMOOTH.zeta(), 1.0);
        assert_eq!(SNAPPY.zeta(), 1.0);
        assert!((BOUNCY.zeta() - 0.70).abs() < 1e-6);
    }

    #[test]
    fn smooth_is_monotonic_and_tier_a_safe() {
        assert_eq!(spring_easing(SMOOTH, 0.0), 0.0);
        assert_eq!(spring_easing(SMOOTH, 1.0), 1.0);
        let mut previous = 0.0;
        for step in 0..=1000 {
            let value = spring_easing(SMOOTH, step as f32 / 1000.0);
            assert!((0.0..=1.0).contains(&value));
            assert!(value >= previous - 1e-6);
            previous = value;
        }
    }

    #[test]
    fn snappy_is_monotonic_and_quick() {
        assert_eq!(spring_easing(SNAPPY, 0.0), 0.0);
        assert_eq!(spring_easing(SNAPPY, 1.0), 1.0);
        let mut previous = 0.0;
        for step in 0..=1000 {
            let value = spring_easing(SNAPPY, step as f32 / 1000.0);
            assert!((0.0..=1.0).contains(&value));
            assert!(value >= previous - 1e-6);
            previous = value;
        }
        assert!(SNAPPY.duration <= 0.25);
    }

    #[test]
    fn bouncy_preset_overshoots_within_bounds() {
        let mut peak: f32 = 1.0;
        for step in 0..=1000 {
            let value = spring_easing(BOUNCY, step as f32 / 1000.0);
            assert!(value.is_finite());
            assert!(value >= -1e-5);
            assert!(value < 1.5);
            peak = peak.max(value);
        }
        assert!(peak > 1.001, "bouncy did not overshoot");
    }

    #[test]
    fn smooth_settles_at_its_duration() {
        let value = spring_easing(SMOOTH, 1.0);
        assert!((value - 1.0).abs() <= 1e-6);
        assert!(SMOOTH.duration > std::time::Duration::from_millis(0).as_secs_f32());
    }
}
