import os
import sys
import json

import subprocess

# latex template
template_file = "base.txt"
template_key = "CONTENTOFEQUATION"

# read the input
input = {
}

# convert escape characters to their non-escaped equivalent
for key, value in input.items():
  value = value.replace("\n", "\\n")
  value = value.replace("\t", "\\t")
  value = value.replace("\r", "\\r")
  value = value.replace("\b", "\\b")
  value = value.replace("\f", "\\f")
  value = value.replace("\v", "\\v")
  value = value.replace("\a", "\\a")
  value = value.replace("\0", "\\0")
  input[key] = value

# read the template
with open(template_file, 'r') as file:
  template_content = file.read()
  # replace the content
  for key, value in sorted(input.items(), reverse=True) :
    iter_content = template_content.replace(template_key, value)
    # write the output
    with open(key +  ".tex", 'w') as file:
      file.write(iter_content)
    # compile the latex with subprocess to surpress the output
    p = subprocess.Popen(["pdflatex", key + ".tex", "-interaction=nonstopmode"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    p.wait()
    # wait for the pdf
    while not os.path.exists(key + ".pdf"):
      pass
    # crop the pdf
    os.system("pdfcrop " + key + ".pdf")
    # remove the pdf
    os.system("rm " + key + ".pdf")
    # rename the cropped pdf
    os.system("mv " + key + "-crop.pdf " + key + ".pdf")
    # crop the pdf
    os.system("pdfcrop " + key + ".pdf")
    # remove the pdf
    os.system("rm " + key + ".pdf")
    # rename the cropped pdf
    os.system("mv " + key + "-crop.pdf " + key + ".pdf")
    # remove the aux files
    os.system("rm " + key + ".aux")
    os.system("rm " + key + ".log")
    os.system("rm " + key + ".tex")
    os.system("rm " + key + ".fls")
    os.system("rm " + key + ".fdb_latexmk")
    # convert to svg
    os.system("inkscape --export-type=\"svg\" " + key + ".pdf")
    # rename .pdf.svg to .svg
    os.system("mv " + key + ".pdf.svg " + key + ".svg")
    # remove the pdf
    #os.system("rm " + key + ".pdf")



