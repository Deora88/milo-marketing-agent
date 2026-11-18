// Builds web/milo.css from the classes used in web/index.html.
// Only needed when you change classes in index.html:
//   npx tailwindcss@3 -c web/tailwind.config.js -i web/tailwind.input.css -o web/milo.css --minify
module.exports = {
  content: [__dirname + "/index.html"],
  theme: {
    extend: {
      colors: {
        bg: "#FAFAFA",
        elevated: "#FFFFFF",
        surface: "#FFFFFF",
        ink: "#18181B",
        muted: "#52525B",
        subtle: "#71717A",
        line: "#E4E4E7",
        "line-strong": "#D4D4D8",
        accent: "#C2410C",
        "accent-fg": "#FFFFFF",
        "accent-soft": "#FFF1EC",
        ok: "#15803D",
        warn: "#B45309",
        danger: "#B91C1C",
        brand: "#F2542D",
      },
      fontFamily: {
        sans: ["Plus Jakarta Sans", "ui-sans-serif", "system-ui", "sans-serif"],
        display: ["Plus Jakarta Sans", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      boxShadow: {
        soft: "0 1px 2px rgb(24 24 27 / 0.06), 0 8px 24px -16px rgb(24 24 27 / 0.25)",
      },
    },
  },
};
