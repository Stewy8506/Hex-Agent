use std::time::Duration;

use super::presets::SpringPreset;

pub const REDUCED_OPACITY_FADE: Duration = Duration::from_millis(180);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MotionKind {
    Spatial,
    Opacity,
}

#[derive(Clone, Copy, Debug)]
pub struct MotionPolicy {
    reduced: bool,
}

impl MotionPolicy {
    pub const fn new(reduced: bool) -> Self {
        Self { reduced }
    }

    pub const fn is_reduced(self) -> bool {
        self.reduced
    }

    pub const fn snaps(self, kind: MotionKind) -> bool {
        self.reduced && matches!(kind, MotionKind::Spatial)
    }

    pub fn duration(self, kind: MotionKind, normal: Duration) -> Duration {
        if self.is_reduced() && kind == MotionKind::Opacity && normal > REDUCED_OPACITY_FADE {
            REDUCED_OPACITY_FADE
        } else {
            normal
        }
    }

    pub fn preset(self, kind: MotionKind, normal: SpringPreset) -> SpringPreset {
        if self.is_reduced() && kind == MotionKind::Opacity {
            SpringPreset::new(
                self.duration(kind, Duration::from_secs_f32(normal.duration))
                    .as_secs_f32(),
                0.0,
            )
        } else {
            normal
        }
    }
}

pub fn reduced_motion() -> bool {
    #[cfg(windows)]
    {
        const SPI_GETCLIENTAREAANIMATION: u32 = 0x1042;
        #[link(name = "user32")]
        extern "system" {
            fn SystemParametersInfoW(
                action: u32,
                uiparam: u32,
                pvparam: *mut core::ffi::c_void,
                winini: u32,
            ) -> i32;
        }
        let mut animations_enabled: i32 = 1;
        let ok = unsafe {
            SystemParametersInfoW(
                SPI_GETCLIENTAREAANIMATION,
                0,
                (&mut animations_enabled as *mut i32).cast(),
                0,
            )
        };
        return ok != 0 && animations_enabled == 0;
    }
    #[cfg(target_os = "macos")]
    {
        use objc2_app_kit::NSWorkspace;
        NSWorkspace::sharedWorkspace().accessibilityDisplayShouldReduceMotion()
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::motion::{BOUNCY, SMOOTH};
    use core::prelude::v1::test;

    #[test]
    fn reduced_policy_snaps_space_but_shortens_opacity() {
        let policy = MotionPolicy::new(true);
        assert!(policy.snaps(MotionKind::Spatial));
        assert!(!policy.snaps(MotionKind::Opacity));
        assert_eq!(
            policy.duration(
                MotionKind::Spatial,
                Duration::from_secs_f32(SMOOTH.duration)
            ),
            Duration::from_secs_f32(SMOOTH.duration)
        );
        assert_eq!(
            policy.duration(
                MotionKind::Opacity,
                Duration::from_secs_f32(BOUNCY.duration)
            ),
            REDUCED_OPACITY_FADE
        );
        assert_eq!(
            policy.preset(MotionKind::Opacity, BOUNCY).duration,
            REDUCED_OPACITY_FADE.as_secs_f32()
        );
        assert_eq!(policy.preset(MotionKind::Opacity, BOUNCY).bounce, 0.0);
    }

    #[test]
    fn full_policy_preserves_presets() {
        let policy = MotionPolicy::new(false);
        assert!(!policy.snaps(MotionKind::Spatial));
        assert_eq!(policy.preset(MotionKind::Opacity, BOUNCY), BOUNCY);
    }

    #[test]
    fn reduced_motion_probe_does_not_panic() {
        let _ = reduced_motion();
    }
}
