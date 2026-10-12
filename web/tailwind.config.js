// Builds web/milo.css from the classes used in web/index.html.
// Only needed when you change classes in index.html:
//   npx tailwindcss@3 -c web/tailwind.config.js -i web/tailwind.input.css -o web/milo.css --minify
module.exports = {
  content: [__dirname + "/index.html"],
  theme: {
    extend: {
      colors: {
        bg: "#f4f1ea",
        elevated: "#fbfaf6",
        surface: "#fffcf7",
        ink: "#1c1b19",
        muted: "#6d6962",
        subtle: "#8f8a81",
        line: "#e4dfd4",
        "line-strong": "#cfc8b8",
        accent: "#35586a",
        "accent-fg": "#f7f4ee",
        "accent-soft": "#e7eef1",
        ok: "#2f6b4f",
        warn: "#8a5a22",
        danger: "#8f3a32",
      },
      fontFamily: {
        sans: ["Outfit", "ui-sans-serif", "system-ui", "sans-serif"],
        display: ["Fraunces", "ui-serif", "Georgia", "serif"],
      },
      boxShadow: {
        soft: "0 1px 2px rgb(28 27 25 / 0.04), 0 10px 28px rgb(28 27 25 / 0.05)",
      },
    },
  },
};
