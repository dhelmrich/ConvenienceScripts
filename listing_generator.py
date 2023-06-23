""" A script that takes a python file and produces a listing PDF of the code with line numbers """

import argparse
import glob
import os
import re
import subprocess
import sys

from pygments import highlight
from pygments.formatters import LatexFormatter
from pygments.lexers import get_lexer_by_name

# Constants
# =========
# The default font size for the listing
DEFAULT_FONT_SIZE = 10
# The default font family for the listing
DEFAULT_FONT_FAMILY = 'courier'
# The default font size for the line numbers
DEFAULT_LINE_NUMBERS_FONT_SIZE = 8
# The default font family for the line numbers
DEFAULT_LINE_NUMBERS_FONT_FAMILY = 'courier'
# The default font size for the line numbers
DEFAULT_LINE_NUMBERS_WIDTH = 0.5
# The default font family for the line numbers
DEFAULT_LINE_NUMBERS_STYLE = 'right'
# The default font size for the line numbers
DEFAULT_LINE_NUMBERS_SKIP = 1
# The default font family for the line numbers
DEFAULT_LINE_NUMBERS_STEP = 1
# The default font size for the line numbers
DEFAULT_LINE_NUMBERS_FIRST = 1

# main function
# =============
if __name__ == '__main__':
    # Parse the command line arguments
    parser = argparse.ArgumentParser(description='Generate a PDF listing of a python file')
    parser.add_argument('--file', default='./*.py', help='The python file to generate a listing for')
    parser.add_argument('--font-size', type=int, default=DEFAULT_FONT_SIZE, help='The font size for the listing')
    parser.add_argument('--font-family', default=DEFAULT_FONT_FAMILY, help='The font family for the listing')
    parser.add_argument('--line-numbers-font-size', type=int, default=DEFAULT_LINE_NUMBERS_FONT_SIZE, help='The font size for the line numbers')
    parser.add_argument('--line-numbers-font-family', default=DEFAULT_LINE_NUMBERS_FONT_FAMILY, help='The font family for the line numbers')
    parser.add_argument('--line-numbers-width', type=float, default=DEFAULT_LINE_NUMBERS_WIDTH, help='The width of the line numbers')
    parser.add_argument('--line-numbers-style', default=DEFAULT_LINE_NUMBERS_STYLE, help='The style of the line numbers')
    parser.add_argument('--line-numbers-skip', type=int, default=DEFAULT_LINE_NUMBERS_SKIP, help='The number of lines to skip between line numbers')
    parser.add_argument('--line-numbers-step', type=int, default=DEFAULT_LINE_NUMBERS_STEP, help='The step between line numbers')
    parser.add_argument('--line-numbers-first', type=int, default=DEFAULT_LINE_NUMBERS_FIRST, help='The number of the first line')
    args = parser.parse_args()

    # check if the file has a wildcard
    if '*' in args.file:
        # get the files that match the wildcard
        files = glob.glob(args.file)
        # if there are more than one file that matches the wildcard
        if len(files) > 1:
            # iterate over the files
            for file in files:
                # replace backslashes with forward slashes
                file = file.replace('\\', '/')
                thisfile = __file__.replace('\\', '/')
                # if the file is this script
                if "listing_generator" in file or thisfile == file:
                    # skip it
                    continue
                print("Generating listing for {}".format(file))
                # call this script with the file
                subprocess.run(['python', __file__,'--file', file])
        else :
            print("Wildcard argument did not match any files")
            exit(1)
        exit(0)

    # Check that the file exists
    if not os.path.isfile(args.file):
        print('The file {} does not exist'.format(args.file))
        sys.exit(1)

    # Check that the file is a python file
    if not re.match(r'.*\.py$', args.file):
        print('The file {} is not a python file'.format(args.file))
        sys.exit(1)

    # Get the directory of the file
    directory = os.path.dirname(args.file)

    # Get the name of the file without the extension
    filename = os.path.splitext(os.path.basename(args.file))[0]

    # Get the name of the output file
    latex_file = os.path.join(directory, '{}.tex'.format(filename))
    output_file = os.path.join(directory, '{}.pdf'.format(filename))

    # Get the lexer for the file
    lexer = get_lexer_by_name('python', stripall=True)

    # add a preamble line to deactivate the page number
    preamble = r'\pagenumbering{gobble}'

    # Get the formatter for the file
    formatter = LatexFormatter(
        font_size=args.font_size,
        font_family=args.font_family,
        linenos=True,
        linenos_font_size=args.line_numbers_font_size,
        linenos_font_family=args.line_numbers_font_family,
        linenos_width=args.line_numbers_width,
        linenos_style=args.line_numbers_style,
        linenos_skip=args.line_numbers_skip,
        linenos_step=args.line_numbers_step,
        linenos_first=args.line_numbers_first,
        full=True,
        preamble=preamble
    )

    code = ''
    # Get the code from the file
    with open(args.file, 'r') as f:
        code = f.read()
    
    # Highlight the code
    latex_code = highlight(code, lexer, formatter)

    # we write in this directory
    with open(latex_file, 'w') as f:
        f.write(latex_code)
    
    command = ['pdflatex', '-interaction', 'nonstopmode', '-output-directory', directory, latex_file]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # remove the aux file
    os.remove(os.path.join(directory, '{}.aux'.format(filename)))
    # remove the log file
    os.remove(os.path.join(directory, '{}.log'.format(filename)))
    # remove the tex file
    os.remove(os.path.join(directory, '{}.tex'.format(filename)))

    # crop the pdf to content using pdfCropMargin
    command = ['pdfCropMargins', '-v', '-a4', '0', '0', '0', '0', output_file]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # remove the original pdf file
    os.remove(output_file)
    # rename the cropped pdf file
    os.rename(os.path.join(directory, '{}_cropped.pdf'.format(filename)), output_file)

    # run pdfCropMargins again to remove a centimeter from the bottom
    command = ['pdfCropMargins', '-v', '-a4', '0', '0', '0', '0', output_file]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # remove the original pdf file
    os.remove(output_file)
    # rename the cropped pdf file
    os.rename(os.path.join(directory, '{}_cropped.pdf'.format(filename)), output_file)



