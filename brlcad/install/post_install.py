"""
The post_install script is the the beating heart of python-brlcad. After
setuptools runs the default install command, it runs the entrypoint ``main``
which generates the python/BRL-CAD bindings.

This needs to be a post-install script because on most python installations,
the setup script is executed with elevated permissons. After installation, such
as during the first run, the script has less than elevated permissions and
can't insert generated files into the installed location. So that's why this is
a part of the setuptools install process.
"""

import os
import json
import logging
import imp
import shutil

import ctypesgencore

from options import load_ctypesgen_options, load_config


def setup_logging(level=logging.DEBUG):
    """
    Dump everything to stdout by default.

    http://docs.python.org/2/howto/logging-cookbook.html
    """
    logger = logging.getLogger("brlcad_post_install")
    logger.setLevel(level)

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter)

    logger.addHandler(ch)
    return logger


def generate_wrapper(ctypesgen_options, logger):
    """
    Generate a ctypes wrapper around a specific library.

    @param ctypesgen_options: the ctypesgencore options
    """
    # parse
    logger.debug("parsing")
    descriptions = ctypesgencore.parser.parse(ctypesgen_options.headers, ctypesgen_options)

    # process
    logger.debug("processing")
    ctypesgencore.processor.process(descriptions, ctypesgen_options)

    # print
    logger.debug("printing")
    ctypesgencore.printer_python.WrapperPrinter(ctypesgen_options.output, ctypesgen_options, descriptions)


def cleanup_bindings_dir(bindings_path, cached_bindings_path, logger):
    """
    Remove any leftover directories from a previous install.
    """

    try:
        # make the _bindings folder
        os.makedirs(bindings_path)
    except OSError:
        logger.debug("_bindings path already exists, deleting it")

        shutil.rmtree(bindings_path)

    # also remove _bindings from the local directory
    try:
        logger.debug("Deleting another _bindings/")
        shutil.rmtree(os.path.join(os.path.dirname(__file__), "_bindings"))
    except Exception:
        logger.debug(
            "_bindings wasn't previously created, so it doesn't need to be "
            "removed."
        )

    # also remove cached _bindings
    try:
        logger.debug("Deleting cached _bindings/")
        shutil.rmtree(cached_bindings_path)
    except Exception:
        logger.debug(
            "_bindings wasn't previously cached, so it doesn't need to be "
            "removed."
        )


def main(library_path, logger=None):
    if not logger:
        logger = setup_logging()

    logger.debug("ctypesgencore version is {0}".format(ctypesgencore.__version__))

    # this is where the generated files are placed
    bindings_path = os.path.join(library_path, "_bindings")
    logger.debug("bindings_path is {0}".format(bindings_path))

    # read configuration
    config = load_config()
    # install from cached directory if the config file requests it and the bindings are already there
    # you will want to enable this in ~/.python-brlcad.cfg when developing higher level features to
    # avoid the time consuming re-installation of the BRL-CAD bindings
    # in the stock python-brlcad.cfg file it will be set to False
    cache_bindings = config.has_option("brlcad", "cached-reinstall") and config.getboolean("brlcad", "cached-reinstall")
    cached_bindings_path = os.path.join(os.path.dirname(__file__), "..", "_bindings")
    if cache_bindings and os.path.isdir(cached_bindings_path):
        logger.debug("installing cached _bindings from {}".format(cached_bindings_path))
        shutil.copytree(cached_bindings_path, bindings_path)
        return
    else:
        cleanup_bindings_dir(bindings_path, cached_bindings_path, logger=logger)
    # find brl-cad installation and set up ctypesgen options
    ctypesgen_library_options, options_map, brlcad_info = load_ctypesgen_options(bindings_path, config, logger)

    # Holds the name of a module and the names that the module defines.
    symbol_map = {}

    # List of libraries that have been generated by the following for loop.
    generated_libraries = []

    for ctypesgen_options in ctypesgen_library_options:
        lib_name = ctypesgen_options.brlcad_lib_name

        logger.debug("Processing library: {0}".format(lib_name))
        logger.debug("Options: {0}".format(ctypesgen_options))

        # HACK: This is how ctypesgen is told to not re-define the same types.
        # The basic concept is to look through dependency_modules and see which
        # modules have already been generated (all of them should be generated
        # by now unless there's a dependency loop in BRL-CAD libraries...). The
        # symbols from each of the generated modules are passed in as
        # other_known_names so that ctypesgen doesn't redefine the
        # previously-generated names. The list of module names is passed in as
        # "modules" so that ctypesgen generates a python file that actually
        # imports those symbols.

        # Construct the list of types and other variables that are already
        # defined by other ctypesgen-generated files.
        ctypesgen_options.other_known_names = []
        for module in ctypesgen_options.modules:
            ctypesgen_options.other_known_names.extend(symbol_map[module])

        # TODO: the ctypesgen printer might need to re-arrange when it imports
        # modules. It should probably happen before the preamble is printed, so
        # that the preamble is just cached instead of always redefining
        # everything. I think now that the preamble is an import line, the
        # types are already cached, so it's probably okay for the moment.

        # generate the wrapper bindings (woot)
        generate_wrapper(ctypesgen_options, logger=logger)
        generated_libraries.append(lib_name)
        logger.debug("Done generating the wrapper file for {0}".format(lib_name))

        # HACK: Load this latest generated library. Create the appropriate
        # __init__.py file and then import the current module. Add module ->
        # dir(module) to the list of known names. On the next pass, if
        # ctypesgen_options.modules has any values, then include the
        # list of known names from the data structure as "other_known_names".

        # 1) generate the appropriate __init__.py file (__all__ will need to be constructed)
        logger.debug("About to write the __init__.py file")
        generate_init_file(bindings_path, generated_libraries, brlcad_info["version"], logger)
        logger.debug("Okay, __init__.py has been updated.")

        # 2) load the latest generated module
        logger.debug("Loading the __init__.py module from {0}".format(bindings_path))
        imp.load_source("_bindings", os.path.join(bindings_path, "__init__.py"))

        logger.debug("Loading the {0} module from {1}.".format(lib_name, ctypesgen_options.output))
        latest_module = imp.load_source(lib_name, ctypesgen_options.output)
        symbols = dir(latest_module)

        # 3) store the list of defined names from that module by running dir(loaded_module)
        symbol_map[lib_name] = symbols

        # TODO: confirm the following TODO statement. It looks like this should
        # be working now?
        # TODO: ctypesgen needs to support "other_known_names" being passed in
        # through options (right now it just overrides this value).

    # always cache bindings, it helps during development and while running python from the brlcad dir:
    logger.debug("Caching _bindings to: {}".format(cached_bindings_path))
    shutil.copytree(bindings_path, cached_bindings_path)


def generate_init_file(bindings_path, library_names, brlcad_version, logger):
    """
    Generates the __init__.py file based on the current list of generated
    wrappers.
    """
    # absolute path to where the __init__.py file should be placed
    init_path = os.path.join(bindings_path, "__init__.py")
    logger.debug("Writing __init__.py to: {0}".format(init_path))

    # Add the BRLCAD_VERSION variable to the exported symbols list:
    if "BRLCAD_VERSION" not in library_names:
        library_names.insert(0, "BRLCAD_VERSION")
    # build the __init__.py file contents
    init_contents = "".join([
        "from distutils.version import StrictVersion\n\n"
        "BRLCAD_VERSION = ", repr(brlcad_version), "\n",
        "__all__ = ", json.dumps(library_names), "\n",
    ])

    # save the init file
    init_file = open(init_path, "w")
    init_file.write(init_contents)
    init_file.close()

    return True
