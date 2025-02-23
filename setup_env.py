import sys
import os

def setup_paths():
    # Get the current working directory (the directory where the notebook is located)
    notebook_dir = os.getcwd()

    # Define the path to the scripts directory
    scripts_dir = os.path.abspath(os.path.join(notebook_dir, '..', 'scripts'))

    # Add the scripts directory to sys.path if it's not already there
    if scripts_dir not in sys.path:
        sys.path.append(scripts_dir)

    print(f"Scripts directory '{scripts_dir}' has been added to sys.path.")

def setup_imports():
    # Import the necessary classes and return them
    from conversion.pdbhandler import PDBFileHandler, Atom
    from cluster.clusternetwork import ClusterNetwork
    from cluster.clusterbatchanalyzer import ClusterBatchAnalyzer

    print("Class imports have been set up.")

    # Return the imported classes
    return PDBFileHandler, Atom, ClusterNetwork, ClusterBatchAnalyzer

def setup_environment():
    # Setup paths and return the imported classes
    setup_paths()
    return setup_imports()
