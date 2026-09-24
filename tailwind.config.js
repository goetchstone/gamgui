// Tailwind v3 config for gamgui/web/static/app.css, built by `make css` (scripts/build_css.sh) and
// committed. A class that appears in none of these files is not in the CSS, so a template edit that
// adds one needs `make css` (CI's lint job fails on a stale app.css).
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: {
    relative: true,
    files: [
      "./gamgui/web/templates/**/*.html",
      "./gamgui/web/static/**/*.js",
      "!./gamgui/web/static/vendor/**",  // third-party, and its tokens would only add noise
      "./gamgui/**/*.py",                // class names built in Python
      "!./gamgui/resources/**",          // the vendored GAM (gitignored, differs per machine)
    ],
  },
  theme: {
    extend: {
      colors: {
        ink: "#141414",        // near-black body text (brand black is #000000)
        paper: "#FAF9F6",      // warm off-white page
        brand: {
          black: "#000000",
          white: "#FFFFFF",
          gray: "#B2B4BB",     // warm gray
          blue: "#69829E",     // slate blue
          blueink: "#52647B",  // darker slate for text/hover contrast on white
        },
      },
      fontFamily: {
        serif: ['"Source Serif 4"', "Minion Pro", "Georgia", "serif"],
        sans: ['"Source Sans 3"', "Myriad Pro", "-apple-system", "BlinkMacSystemFont", "system-ui", "sans-serif"],
      },
    },
  },
};
