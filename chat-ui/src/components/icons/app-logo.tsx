import Image from "next/image";
import { cn } from "@/lib/utils";

// public/logo.png is 900x512 (network head + AGENTIC6G wordmark)
const LOGO_ASPECT_RATIO = 900 / 512;

export function AppLogo({
  className,
  height = 48,
}: {
  className?: string;
  height?: number;
}) {
  return (
    <Image
      src="/logo.png"
      alt="AGENTIC6G logo"
      width={Math.round(height * LOGO_ASPECT_RATIO)}
      height={height}
      className={cn("shrink-0", className)}
      priority
    />
  );
}
