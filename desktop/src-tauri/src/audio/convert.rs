//! Converts audio in the PCM format a device delivers to the wire format (16 kHz mono int16), keeping
//! resampler state between buffers. The counterpart of WireConverter in AudioWire.swift.
//!
//! Resampling is plain: an integer ratio (48 kHz → 16 kHz) averages each group of samples, anything else
//! interpolates linearly. Good enough for speech; a windowed-sinc filter can replace it later.

use crate::wire::SAMPLE_RATE;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sample {
    I16,
    /// 32-bit integer samples, or 24-bit ones in 32-bit containers (the top bits are what matter).
    I32,
    F32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Format {
    pub rate: u32,
    pub channels: u16,
    pub sample: Sample,
    /// Bytes per frame (all channels).
    pub block_align: u16,
}

impl Format {
    pub const WIRE: Format = Format { rate: SAMPLE_RATE as u32, channels: 1, sample: Sample::I16, block_align: 2 };

    pub fn is_wire(&self) -> bool {
        *self == Format::WIRE
    }

    pub fn describe(&self) -> String {
        let kind = match self.sample {
            Sample::I16 => "int16",
            Sample::I32 => "int32",
            Sample::F32 => "float32",
        };
        format!("{} Hz, {} ch, {kind}", self.rate, self.channels)
    }
}

pub struct Converter {
    from: Format,
    /// Input samples per output sample.
    step: f64,
    /// For an integer ratio: input samples not yet averaged into an output sample.
    leftover: Vec<f32>,
    /// For interpolation: the last input sample of the previous buffer, and how far past it the next
    /// output sample falls (in input samples).
    prev: f32,
    pos: f64,
}

impl Converter {
    pub fn new(from: Format) -> Converter {
        Converter {
            from,
            step: from.rate as f64 / SAMPLE_RATE as f64,
            leftover: Vec::new(),
            prev: 0.0,
            pos: 1.0,
        }
    }

    /// Whole frames of the input format in, wire samples out.
    pub fn convert(&mut self, bytes: &[u8]) -> Vec<i16> {
        let mono = self.mono(bytes);
        let out = if self.from.rate == SAMPLE_RATE as u32 {
            mono
        } else if self.from.rate % SAMPLE_RATE as u32 == 0 {
            self.decimate(mono)
        } else {
            self.interpolate(mono)
        };
        out.into_iter().map(|s| (s * 32768.0).round().clamp(-32768.0, 32767.0) as i16).collect()
    }

    /// Each frame mixed down to one float sample in -1…1.
    fn mono(&self, bytes: &[u8]) -> Vec<f32> {
        let channels = self.from.channels.max(1) as usize;
        let width = match self.from.sample {
            Sample::I16 => 2,
            Sample::I32 | Sample::F32 => 4,
        };
        let frame = (self.from.block_align as usize).max(channels * width);
        let mut out = Vec::with_capacity(bytes.len() / frame);
        for chunk in bytes.chunks_exact(frame) {
            let mut sum = 0.0f32;
            for c in 0..channels {
                let at = c * width;
                sum += match self.from.sample {
                    Sample::I16 => i16::from_le_bytes([chunk[at], chunk[at + 1]]) as f32 / 32768.0,
                    Sample::I32 => {
                        i32::from_le_bytes([chunk[at], chunk[at + 1], chunk[at + 2], chunk[at + 3]]) as f32 / 2147483648.0
                    }
                    Sample::F32 => f32::from_le_bytes([chunk[at], chunk[at + 1], chunk[at + 2], chunk[at + 3]]),
                };
            }
            out.push(sum / channels as f32);
        }
        out
    }

    fn decimate(&mut self, mono: Vec<f32>) -> Vec<f32> {
        let n = self.step as usize;
        let mut all = std::mem::take(&mut self.leftover);
        all.extend(mono);
        let whole = all.len() / n * n;
        let out = all[..whole].chunks_exact(n).map(|group| group.iter().sum::<f32>() / n as f32).collect();
        self.leftover = all[whole..].to_vec();
        out
    }

    fn interpolate(&mut self, mono: Vec<f32>) -> Vec<f32> {
        let mut buf = Vec::with_capacity(mono.len() + 1);
        buf.push(self.prev);
        buf.extend(mono);
        let mut out = Vec::with_capacity((buf.len() as f64 / self.step) as usize + 1);
        let mut p = self.pos;
        while p + 1.0 < buf.len() as f64 {
            let i = p as usize;
            let frac = (p - i as f64) as f32;
            out.push(buf[i] * (1.0 - frac) + buf[i + 1] * frac);
            p += self.step;
        }
        self.prev = *buf.last().unwrap();
        self.pos = p - (buf.len() as f64 - 1.0);
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn i16_bytes(samples: &[i16]) -> Vec<u8> {
        samples.iter().flat_map(|s| s.to_le_bytes()).collect()
    }

    fn f32_bytes(samples: &[f32]) -> Vec<u8> {
        samples.iter().flat_map(|s| s.to_le_bytes()).collect()
    }

    #[test]
    fn the_wire_format_passes_straight_through() {
        let mut c = Converter::new(Format::WIRE);
        assert_eq!(c.convert(&i16_bytes(&[1, -2, 32767])), vec![1, -2, 32767]);
    }

    #[test]
    fn stereo_48k_int16_is_mixed_down_and_decimated_by_three() {
        let format = Format { rate: 48000, channels: 2, sample: Sample::I16, block_align: 4 };
        let mut c = Converter::new(format);
        // Left 3000, right 1000 in every frame: mono 2000, unchanged by averaging.
        let frames: Vec<i16> = (0..48).flat_map(|_| [3000i16, 1000]).collect();
        let out = c.convert(&i16_bytes(&frames));
        assert_eq!(out.len(), 16);
        assert!(out.iter().all(|&s| s == 2000), "{out:?}");
        // Frames that don't fill a group carry over to the next buffer.
        let out = c.convert(&i16_bytes(&[3000, 1000, 3000, 1000]));
        assert_eq!(out.len(), 0);
        let out = c.convert(&i16_bytes(&[3000, 1000]));
        assert_eq!(out, vec![2000]);
    }

    #[test]
    fn float_44100_is_interpolated_to_16k() {
        let format = Format { rate: 44100, channels: 1, sample: Sample::F32, block_align: 4 };
        let mut c = Converter::new(format);
        let input = vec![0.5f32; 44100];
        let out = c.convert(&f32_bytes(&input));
        assert!((out.len() as i64 - 16000).abs() <= 1, "{}", out.len());
        assert!(out[1..].iter().all(|&s| s == 16384), "{:?}", &out[..5]);
    }

    #[test]
    fn chunked_input_gives_the_same_output_as_one_buffer() {
        let format = Format { rate: 44100, channels: 1, sample: Sample::I16, block_align: 2 };
        let signal: Vec<i16> = (0..4410).map(|i| ((i as f32 * 0.05).sin() * 10000.0) as i16).collect();
        let mut whole = Converter::new(format);
        let expected = whole.convert(&i16_bytes(&signal));
        let mut chunked = Converter::new(format);
        let mut got = Vec::new();
        for chunk in signal.chunks(441) {
            got.extend(chunked.convert(&i16_bytes(chunk)));
        }
        assert_eq!(got, expected);
        assert!((got.len() as i64 - 1600).abs() <= 1);
    }

    #[test]
    fn int32_samples_use_their_top_bits() {
        let format = Format { rate: 16000, channels: 1, sample: Sample::I32, block_align: 4 };
        let mut c = Converter::new(format);
        let bytes: Vec<u8> = [i32::MAX / 2 + 1, i32::MIN].iter().flat_map(|s| s.to_le_bytes()).collect();
        assert_eq!(c.convert(&bytes), vec![16384, -32768]);
    }
}
