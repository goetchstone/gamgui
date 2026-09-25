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
        // Text must clear WCAG AA (4.5:1) on paper and white; tests/test_contrast.py checks every text
        // class app.css holds. brand-gray is 2:1 there, so borders only; no /70-style opacity on text.
        brand: {
          black: "#000000",
          white: "#FFFFFF",
          gray: "#B2B4BB",     // warm gray: borders, rules, tints — never text
          field: "#85878E",    // the gray, darkened for a form control's border: 3:1 (WCAG 1.4.11) on paper and white
          grayink: "#6D6E75",  // the gray, darkened for text (helper text, captions, empty states)
          blue: "#5A728E",     // slate blue, darkened from #69829E so its text and white-on-blue pass
          blueink: "#52647B",  // darker slate for text/hover
        },
      },
      fontFamily: {
        serif: ['"Source Serif 4"', "Minion Pro", "Georgia", "serif"],
        sans: ['"Source Sans 3"', "Myriad Pro", "-apple-system", "BlinkMacSystemFont", "system-ui", "sans-serif"],
      },
    },
  },
};
