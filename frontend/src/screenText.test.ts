// @vitest-environment jsdom
import { beforeAll, expect, test } from "vitest";
import { screenText } from "./screenText";

beforeAll(() => {
  // jsdom lays nothing out, so everything would count as off screen; call it all visible.
  Element.prototype.getBoundingClientRect = () =>
    ({ width: 100, height: 20, top: 10, bottom: 30, left: 10, right: 110, x: 10, y: 10, toJSON: () => ({}) }) as DOMRect;
});

test("the app's own words are kept, personal parts are replaced by a marker, the report panel is left out", () => {
  document.body.innerHTML = `
    <div>
      <h1>Settings</h1>
      <p>Pack your <span>preferences</span> once.</p>
      <div data-private="meeting title">Budget review with Sam</div>
      <div data-private="transcript"><p>Line one</p><p>Line two</p></div>
      <div data-report-ui><p>Describe the problem</p></div>
      <p aria-hidden="true">decorative</p>
      <script>var secret = 1;</script>
    </div>`;
  expect(screenText().split("\n")).toEqual([
    "Settings",
    "Pack your preferences once.",
    "[meeting title hidden]",
    "[transcript hidden]",
  ]);
});

test("repeated lines and empty text are dropped", () => {
  document.body.innerHTML = "<div><p>Same</p><p>Same</p><p>   </p><p>Other</p></div>";
  expect(screenText()).toBe("Same\nOther");
});
