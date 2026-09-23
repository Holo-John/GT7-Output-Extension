// bridge.js
const mesh = require("./mesh_eval.js");

// Map of functions you want to expose
const API = {
  evaluatePatch: mesh.evaluatePatch,
  evaluateCoons: mesh.evaluateCoons,
  evaluateTensor: mesh.evaluateTensor,
  bernstein: mesh.bernstein,
  interpolateCorners: mesh.interpolateCorners,
  normalizeUV: mesh.normalizeUV,
};

function run() {
  const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
  const fn = input.fn;

  if (!API[fn]) {
    throw new Error("Unknown function: " + fn);
  }

  const result = API[fn](...input.args);
  process.stdout.write(JSON.stringify(result));
}

run();
