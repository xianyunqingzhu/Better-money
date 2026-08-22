import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.bettermoney.app",
  appName: "Better-money",
  webDir: "dist",
  server: {
    androidScheme: "https",
  },
  plugins: {
    CapacitorHttp: {
      enabled: true,
    },
    Camera: {
      presentationStyle: "popover",
    },
  },
  android: {
    allowMixedContent: false,
    backgroundColor: "#F6F4EF",
  },
};

export default config;
