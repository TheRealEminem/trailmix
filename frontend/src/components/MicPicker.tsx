import { useCallback, useEffect, useState } from "react";
import { MicIcon } from "./icons";
import { ControlRow, Select } from "./ui";

const STORAGE_KEY = "trailmix.micDeviceId";

export function savedMicId(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

interface Props {
  value: string;
  onChange: (deviceId: string) => void;
}

export default function MicPicker({ value, onChange }: Props) {
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);

  const refresh = useCallback(async () => {
    const all = await navigator.mediaDevices.enumerateDevices();
    // "default" / "communications" are aliases of a real device; listing them would duplicate it.
    setDevices(all.filter((d) => d.kind === "audioinput" && d.deviceId !== "default" && d.deviceId !== "communications"));
  }, []);

  useEffect(() => {
    void refresh();
    navigator.mediaDevices.addEventListener("devicechange", refresh);
    return () => navigator.mediaDevices.removeEventListener("devicechange", refresh);
  }, [refresh]);

  // Chrome hides device names until the site has microphone permission.
  const namesHidden = devices.length > 0 && devices.every((d) => !d.label);

  const unlockNames = async () => {
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true });
      s.getTracks().forEach((t) => t.stop());
    } catch {
      /* permission denied: the list simply stays unnamed */
    }
    await refresh();
  };

  const choose = (id: string) => {
    onChange(id);
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch {
      /* private mode etc. */
    }
  };

  // A remembered device that's no longer plugged in shouldn't show as selected.
  const known = value === "" || devices.some((d) => d.deviceId === value);

  return (
    <ControlRow
      wrap
      icon={<MicIcon size={16} />}
      label="Microphone"
      hint={
        namesHidden ? (
          <button onClick={unlockNames} className="text-left font-medium text-forest underline-offset-2 hover:underline">
            Show device names
          </button>
        ) : (
          "Your side of the conversation"
        )
      }
    >
      <Select
        value={known ? value : ""}
        onChange={choose}
        label="Microphone"
        size="sm"
        className="w-full sm:w-56"
        options={[
          { value: "", label: "System default" },
          ...devices.map((d, i) => ({ value: d.deviceId, label: d.label || `Microphone ${i + 1}` })),
        ]}
      />
    </ControlRow>
  );
}
