#!/usr/bin/python3
"""DSM executes this file using the original session CGI environment."""
import os
import sys

ui = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(ui))
from gateway import main

main(os.path.join(ui, 'gateway.json'))
