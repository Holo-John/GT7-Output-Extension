class GT7TestResult:
    def __init__(self, score, threshold, input_svg, output_svg, input_png, output_png, diff_png):
        self.score = score
        self.threshold = threshold
        self.input_svg = input_svg
        self.output_svg = output_svg
        self.input_png = input_png
        self.output_png = output_png
        self.diff_png = diff_png
