import os
import shutil
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import xraydb
import numpy as np
from scipy.spatial import ConvexHull
from scipy.linalg import eigh
from collections import defaultdict
import matplotlib.pyplot as plt
from tqdm import tqdm
from tqdm.notebook import tqdm  # Import for Jupyter Notebook visual progress bar
from mendeleev import element  # To fetch ionic radii
from datetime import datetime

from conversion.pdbhandler import PDBFileHandler

class ClusterBatchAnalyzer:
    def __init__(self, pdb_directory, target_elements, neighbor_elements, distance_thresholds, 
                 charges, core_residue_names=['PBI'], shell_residue_names=['DMS'], 
                 volume_method='ionic_radius', copy_no_target_files=False):
        self.pdb_directory = pdb_directory
        self.target_elements = target_elements
        self.neighbor_elements = neighbor_elements
        self.distance_thresholds = distance_thresholds
        self.charges = charges
        self.core_residue_names = core_residue_names
        self.shell_residue_names = shell_residue_names
        self.pdb_files = self._load_pdb_files()
        self.cluster_data = []
        self.cluster_size_distribution = defaultdict(list)
        self.volume_method = volume_method
        self.copy_no_target_files = copy_no_target_files
        self.no_target_atoms_files = []
        
        # Only build the ionic radius lookup table if required
        if self.volume_method == 'ionic_radius':
            self.radius_lookup, _ = self.build_ionic_radius_lookup()

    ## -- Supporting Methods
    def _load_pdb_files(self):
        return [os.path.join(self.pdb_directory, f) for f in os.listdir(self.pdb_directory) if f.endswith('.pdb')]

    def get_atomic_number(self, element):
        """
        Returns the atomic number of a given element.

        Parameters:
        - element: str, chemical symbol of the element.
        
        Returns:
        - atomic_number: int, atomic number of the element.
        """
        periodic_table = {
            'H': 1, 'He': 2, 'Li': 3, 'Be': 4, 'B': 5, 'C': 6, 'N': 7, 'O': 8, 'F': 9, 'Ne': 10,
            'Na': 11, 'Mg': 12, 'Al': 13, 'Si': 14, 'P': 15, 'S': 16, 'Cl': 17, 'Ar': 18,
            'K': 19, 'Ca': 20, 'Sc': 21, 'Ti': 22, 'V': 23, 'Cr': 24, 'Mn': 25, 'Fe': 26, 
            'Co': 27, 'Ni': 28, 'Cu': 29, 'Zn': 30, 'Ga': 31, 'Ge': 32, 'As': 33, 'Se': 34, 
            'Br': 35, 'Kr': 36, 'Rb': 37, 'Sr': 38, 'Y': 39, 'Zr': 40, 'Nb': 41, 'Mo': 42,
            'Tc': 43, 'Ru': 44, 'Rh': 45, 'Pd': 46, 'Ag': 47, 'Cd': 48, 'In': 49, 'Sn': 50, 
            'Sb': 51, 'Te': 52, 'I': 53, 'Xe': 54, 'Cs': 55, 'Ba': 56, 'La': 57, 'Ce': 58,
            'Pr': 59, 'Nd': 60, 'Pm': 61, 'Sm': 62, 'Eu': 63, 'Gd': 64, 'Tb': 65, 'Dy': 66, 
            'Ho': 67, 'Er': 68, 'Tm': 69, 'Yb': 70, 'Lu': 71, 'Hf': 72, 'Ta': 73, 'W': 74,
            'Re': 75, 'Os': 76, 'Ir': 77, 'Pt': 78, 'Au': 79, 'Hg': 80, 'Tl': 81, 'Pb': 82, 
            'Bi': 83, 'Po': 84, 'At': 85, 'Rn': 86, 'Fr': 87, 'Ra': 88, 'Ac': 89, 'Th': 90,
            'Pa': 91, 'U': 92, 'Np': 93, 'Pu': 94, 'Am': 95, 'Cm': 96, 'Bk': 97, 'Cf': 98,
            'Es': 99, 'Fm': 100, 'Md': 101, 'No': 102, 'Lr': 103, 'Rf': 104, 'Db': 105, 
            'Sg': 106, 'Bh': 107, 'Hs': 108, 'Mt': 109, 'Ds': 110, 'Rg': 111, 'Cn': 112, 
            'Nh': 113, 'Fl': 114, 'Mc': 115, 'Lv': 116, 'Ts': 117, 'Og': 118
        }
        return periodic_table[element]
    
    @staticmethod
    def determine_safe_thread_count(task_type='cpu', max_factor=2):
        ''' Evaluate the number of threads available for an io-bound or cpu-bound task. '''
        num_cores = os.cpu_count() or 1  # Fallback to 1 if os.cpu_count() returns None

        if task_type == 'cpu':
            # For CPU-bound tasks: use a minimum of 1 and a maximum of num_cores
            thread_count = max(1, num_cores - 1)
        elif task_type == 'io':
            # For I/O-bound tasks: consider using more threads
            thread_count = max(1, num_cores * max_factor)
        else:
            raise ValueError("task_type must be 'cpu' or 'io'")

        return thread_count
    
    ## -- Cluster Analysis Methods

    def analyze_clusters(self, shape_type='sphere', output_folder='no_target_atoms', copy_no_target_files=False):
        all_cluster_sizes = []
        coordination_stats_per_size = defaultdict(lambda: defaultdict(list))
        no_target_atoms_count = 0
        electron_lookup = {}  # Reusable electron lookup table

        if copy_no_target_files:
            os.makedirs(output_folder, exist_ok=True)

        num_threads = self.determine_safe_thread_count(task_type='cpu')

        def process_pdb_file(pdb_file):
            try:
                pdb_handler = PDBFileHandler(pdb_file, core_residue_names=self.core_residue_names, 
                                            shell_residue_names=self.shell_residue_names)

                target_atoms = [atom for atom in pdb_handler.core_atoms if atom.element in self.target_elements]

                if not target_atoms:
                    self.no_target_atoms_files.append(pdb_file)
                    return None, None, None, None, None

                cluster_size = len(target_atoms)
                coordination_stats, _ = self.calculate_coordination_numbers(pdb_handler, target_atoms)

                # Initialize cluster_charge
                cluster_charge = None

                if self.volume_method == 'ionic_radius':
                    cluster_volume = self.estimate_total_molecular_volume(pdb_handler)
                elif self.volume_method == 'radius_of_gyration':
                    atom_charges = [self.charges[atom.element][0] for atom in target_atoms]
                    rg_calculator = RadiusOfGyrationCalculator(
                        atom_positions=[atom.coordinates for atom in target_atoms],
                        atom_elements=[atom.element for atom in target_atoms],
                        atom_charges=atom_charges,
                        electron_lookup=electron_lookup  # Pass the reusable lookup table
                    )
                    if shape_type == 'sphere':
                        cluster_volume = rg_calculator.calculate_volume(method='sphere')
                    elif shape_type == 'ellipsoid':
                        cluster_volume, Rgx, Rgy, Rgz = rg_calculator.calculate_volume(method='ellipsoid')
                        # Make sure to include Rgx, Rgy, and Rgz in the return statement below if you need to store them
                    else:
                        raise ValueError(f"Unknown shape type: {shape_type}")
                else:
                    raise ValueError(f"Unknown volume method: {self.volume_method}")

                # Calculate cluster charge
                cluster_charge = self.calculate_cluster_charge(pdb_handler)

                return pdb_file, cluster_size, coordination_stats, cluster_volume, cluster_charge
            except Exception as e:
                print(f"Error processing file {pdb_file}: {e}")
                return None, None, None, None, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = {executor.submit(process_pdb_file, pdb_file): pdb_file for pdb_file in self.pdb_files}
            for future in tqdm(concurrent.futures.as_completed(futures), total=len(futures), desc="Processing PDB files", ncols=100):
                result = future.result()
                if result[0] is not None:
                    pdb_file, cluster_size, coordination_stats, cluster_volume, cluster_charge = result
                    all_cluster_sizes.append(cluster_size)
                    for pair, (avg_coord, _) in coordination_stats.items():
                        coordination_stats_per_size[cluster_size][pair].append(avg_coord)

                    self.cluster_data.append({
                        'pdb_file': pdb_file,
                        'cluster_size': cluster_size,
                        'coordination_stats': coordination_stats,
                        'volume': cluster_volume,
                        'charge': cluster_charge
                    })
                    self.cluster_size_distribution[cluster_size].append(cluster_volume)
                else:
                    no_target_atoms_count += 1

        if self.copy_no_target_files:
            for pdb_file in self.no_target_atoms_files:
                shutil.copy(pdb_file, output_folder)

        print(f"Number of files without target atoms: {no_target_atoms_count}")

        self.generate_statistics()

        self.plot_cluster_size_distribution(all_cluster_sizes)
        self.plot_coordination_histogram(coordination_stats_per_size)
        if self.volume_method == 'ionic_radius':
            self.plot_average_volume_vs_cluster_size()
        elif self.volume_method == 'radius_of_gyration':
            self.plot_average_volume_vs_cluster_size_rg()
        self.plot_volume_percentage_of_scatterers(box_size_angstroms=53.4, num_boxes=250)
        self.plot_phi_Vc_vs_cluster_size()

        return coordination_stats_per_size

    def calculate_volume_using_rg(self, pdb_handler, shape_type='sphere'):
        """
        Calculate the volume of the cluster using the radius of gyration.
        
        :param pdb_handler: The PDBFileHandler object for the current PDB file.
        :param shape_type: 'sphere' or 'ellipsoid' to choose the volume calculation method.
        :return: The calculated volume.
        """
        # Load data into the RadiusOfGyrationCalculator
        self.rg_calculator.load_from_pdb(pdb_handler, self.charges)
        
        # Calculate volume based on the specified shape type
        if shape_type == 'sphere':
            return self.rg_calculator.calculate_volume(method='sphere')
        elif shape_type == 'ellipsoid':
            return self.rg_calculator.calculate_volume(method='ellipsoid')
        else:
            raise ValueError(f"Unknown shape type: {shape_type}")

    def generate_statistics(self):
        """
        Generate statistics for cluster size distribution, calculating the average and standard deviation of volumes.
        """
        cluster_size_distribution = defaultdict(list)
        
        for data in self.cluster_data:
            cluster_size = data['cluster_size']
            cluster_volume = data['volume']
            
            cluster_size_distribution[cluster_size].append(cluster_volume)
        
        average_volumes = {}
        for size, volumes in cluster_size_distribution.items():
            average_volumes[size] = np.mean(volumes)
        
        self.average_volumes_per_size = average_volumes
        self.cluster_size_distribution = cluster_size_distribution

    ## -- Coordination Number Calculation
    def calculate_coordination_numbers(self, pdb_handler, target_atoms):
        """
        Calculates the coordination numbers and their standard deviations for each atom pair type.

        Parameters:
        - pdb_handler: PDBFileHandler object containing all atoms.
        - target_atoms: List of target atoms to calculate coordination numbers for.

        Returns:
        - coordination_stats: Dictionary containing average and standard deviation of coordination numbers for each atom pair.
        """
        coordination_numbers = defaultdict(list)
        
        for atom in target_atoms:
            counts = {neighbor: 0 for neighbor in self.neighbor_elements}
            for other_atom in pdb_handler.core_atoms + pdb_handler.shell_atoms:
                if other_atom.element in self.neighbor_elements:
                    pair = (atom.element, other_atom.element)
                    if pair in self.distance_thresholds:
                        if self.are_connected(atom, other_atom, self.distance_thresholds[pair]):
                            counts[other_atom.element] += 1
            total_coordination = 0
            for neighbor, count in counts.items():
                coordination_numbers[(atom.element, neighbor)].append(count)
                total_coordination += count
        
        # Calculate mean and standard deviation for each atom pair type
        coordination_stats = {}
        for pair, counts in coordination_numbers.items():
            avg = np.mean(counts)
            std = np.std(counts)
            coordination_stats[pair] = (avg, std)

        return coordination_stats, None

    def are_connected(self, atom1, atom2, threshold):
        distance = np.linalg.norm(np.array(atom1.coordinates) - np.array(atom2.coordinates))
        return distance <= threshold

    def print_coordination_numbers(self, coordination_stats_per_size):
        for size, stats in coordination_stats_per_size.items():
            print(f"Cluster Size: {size}")
            total = 0
            for pair, avg_values in stats.items():
                avg = np.mean(avg_values)
                print(f"  {pair[0]} - {pair[1]}: Avg = {avg:.2f}")
                total += avg
            print(f"  Total Coordination Number: {total:.2f}\n")

    ## -- Cluster Charge Calculation
    def calculate_cluster_charge(self, pdb_handler):
        """
        Calculates the total charge of the cluster by summing the charges of all atoms.
        
        Parameters:
        - pdb_handler: PDBFileHandler object containing all atoms.
        
        Returns:
        - total_charge: The total charge of the cluster.
        """
        total_charge = 0.0
        
        # Sum the charges for core atoms
        for atom in pdb_handler.core_atoms:
            charge, _ = self.charges.get(atom.element, (0, 0))  # Get the charge part of the tuple, default to 0 if not found
            total_charge += charge
        
        # Sum the charges for neighboring atoms
        for atom in pdb_handler.shell_atoms:
            charge, _ = self.charges.get(atom.element, (0, 0))  # Get the charge part of the tuple, default to 0 if not found
            total_charge += charge
        
        # print(f"Total charge of the cluster: {total_charge}")
        return total_charge

    ## -- Volume Calculation Methods -- ##
    # - Calculate Radius of Gyration for Volume Method
    def calculate_radius_of_gyration(self, atom_positions, electron_counts):
        """
        Calculate the radius of gyration (Rg) for a cluster of atoms.

        Parameters:
        - atom_positions: List of (x, y, z) tuples representing atomic positions.
        - electron_counts: List of electron counts corresponding to the atomic positions.

        Returns:
        - radius_of_gyration: The calculated radius of gyration.
        """
        # Calculate the center of mass using electron counts as weights
        electron_counts = np.array(electron_counts)
        positions = np.array(atom_positions)
        center_of_mass = np.average(positions, axis=0, weights=electron_counts)

        # Calculate the radius of gyration
        squared_distances = np.sum(electron_counts * np.sum((positions - center_of_mass) ** 2, axis=1))
        total_electrons = np.sum(electron_counts)
        radius_of_gyration = np.sqrt(squared_distances / total_electrons)

        return radius_of_gyration

    def get_electron_counts(self, atoms):
        """
        Calculate the electron counts for each atom based on the element and formal charge.

        Parameters:
        - atoms: List of atom objects with element and charge information.

        Returns:
        - electron_counts: List of electron counts for each atom.
        """
        electron_counts = []
        for atom in atoms:
            elem_info = element(atom.element)
            total_electrons = elem_info.electrons - self.charges.get(atom.element, (0, 0))[0]
            electron_counts.append(total_electrons)
        return electron_counts

    def estimate_volume_using_rg(self, pdb_handler):
        """
        Estimate the molecular volume using the radius of gyration (Rg).

        Parameters:
        - pdb_handler: PDBFileHandler object containing all atoms.

        Returns:
        - volume: Estimated volume based on Rg in cubic angstroms.
        """
        atom_positions = [atom.coordinates for atom in pdb_handler.core_atoms + pdb_handler.shell_atoms]
        electron_counts = self.get_electron_counts(pdb_handler.core_atoms + pdb_handler.shell_atoms)

        rg = self.calculate_radius_of_gyration(atom_positions, electron_counts)
        volume = (4/3) * np.pi * (rg ** 3)  # Volume estimation using Rg

        return volume
    
    # - Ionic Radius Volume Approximation Method
    # Note: Add a method to base the ionic radius dynamically on the coordination for select atoms.
    def build_ionic_radius_lookup(self):
        """
        Builds a lookup table for ionic radii, electron count, and volume of ions as spheres.
        Uses the self.charges dictionary provided during class initialization, which now includes both charge and coordination number.
        """
        def to_roman(n):
            roman_numerals = {
                1: 'I', 2: 'II', 3: 'III', 4: 'IV', 5: 'V',
                6: 'VI', 7: 'VII', 8: 'VIII', 9: 'IX', 10: 'X',
                11: 'XI', 12: 'XII', 13: 'XIII', 14: 'XIV', 15: 'XV'
            }
            return roman_numerals.get(n, None)

        radius_lookup = {}
        radii_list = []

        for atom_type, (charge, coordination) in self.charges.items():
            # Generate a unique key based on element and charge
            key = (atom_type, charge)
            
            print(f"Looking up ionic radius for {atom_type} with charge {charge} and coordination {coordination}...")

            # Convert numeric coordination number to Roman numeral
            roman_coordination = to_roman(coordination)
            if not roman_coordination:
                print(f"Warning: Invalid coordination number {coordination} for {atom_type}.")
                continue

            if key not in radius_lookup:
                # Retrieve the element information
                elem = element(atom_type)
                print(f"Element data retrieved for {atom_type}: {elem}")
                
                # Debug: Print available ionic radii data
                print(f"Available ionic radii for {atom_type}:")
                for ir in elem.ionic_radii:
                    print(f"  Charge: {ir.charge}, Coordination: {ir.coordination}, Radius: {ir.ionic_radius} pm")
                
                # Get the ionic radii for the specified charge and Roman numeral coordination number
                matching_radius = next(
                    (ir for ir in elem.ionic_radii
                    if ir.charge == charge and ir.coordination == roman_coordination), None
                )
                
                if matching_radius:
                    radius = matching_radius.ionic_radius / 100.0  # Convert pm to Å
                    volume = (4/3) * np.pi * (radius ** 3)  # Calculate the volume of the sphere
                    radius_lookup[key] = {
                        'ionic_radius': radius,
                        'volume': volume  # Store the volume
                    }
                    radii_list.append(radius)
                    print(f"Radius found for {atom_type} with charge {charge} and coordination {roman_coordination}: {radius} Å")
                else:
                    print(f"No ionic radius found for {atom_type} with charge {charge} and coordination {roman_coordination}. Trying covalent radius.")
                    # Fallback to covalent radius
                    covalent_radius = elem.covalent_radius / 100.0  # Convert pm to Å
                    if covalent_radius:
                        volume = (4/3) * np.pi * (covalent_radius ** 3)
                        radius_lookup[key] = {
                            'ionic_radius': covalent_radius,
                            'volume': volume
                        }
                        print(f"Using covalent radius for {atom_type}: {covalent_radius} Å")
                    else:
                        print(f"Warning: No radius found for {atom_type} with charge {charge}.")
                        radius_lookup[key] = {
                            'ionic_radius': None,
                            'volume': None
                        }
                        radii_list.append(None)

        return radius_lookup, np.array(radii_list, dtype=np.float64)

    def estimate_total_molecular_volume(self, pdb_handler):
        """
        Estimates the total molecular volume by summing the volumes of spheres corresponding to each ionic radius.
        Weights the volume by the count of each element.
        
        Parameters:
        - pdb_handler: PDBFileHandler object containing all atoms.
        
        Returns:
        - total_volume: Estimated total molecular volume in cubic angstroms.
        """
        element_counts = defaultdict(int)
        
        all_atoms = pdb_handler.core_atoms + pdb_handler.shell_atoms  # Combine core and shell atoms
        
        # print(f"Calculating volume for PDB file: {pdb_handler.filepath}")
        # print(f"Total atoms found (core + shell): {len(all_atoms)}")
        
        # Count the occurrences of each element type in the PDB file
        for atom in all_atoms:
            key = (atom.element, self.charges.get(atom.element, (0, 0))[0])  # Use provided charges and default to (0, 0) if not found
            element_counts[key] += 1
        
        # # Output the elements and their counts
        # for key, count in element_counts.items():
            # print(f"Element: {key[0]}, Charge: {key[1]}, Count: {count}")
        
        # Calculate the total volume
        total_volume = 0.0
        for key, count in element_counts.items():
            if key in self.radius_lookup:
                if self.radius_lookup[key]['volume'] is not None:
                    weighted_volume = count * self.radius_lookup[key]['volume']
                    total_volume += weighted_volume
                    # print(f"Adding {count} * {self.radius_lookup[key]['volume']} for {key[0]} to total volume.")
                else:
                    print(f"Warning: Volume for {key[0]} with charge {key[1]} is None.")
            else:
                print(f"Warning: No radius found for {key[0]} with charge {key[1]} in lookup table.")
        
        # print(f"Total atoms used in volume calculation: {sum(element_counts.values())}")
        # print(f"Calculated total volume: {total_volume} cubic angstroms\n")
        
        return total_volume
    
    # - Convex Hull Method
    def calculate_cluster_volume(self, pdb_handler):
        all_atoms = pdb_handler.core_atoms + pdb_handler.shell_atoms
        if len(all_atoms) < 4:
            print(f"Not enough atoms to calculate Convex Hull. Returning 0 volume.")
            return 0.0
        
        points = np.array([atom.coordinates for atom in all_atoms])
        hull = ConvexHull(points)
        return hull.volume
    
    def check_cluster_volume(self):
        """
        Loops through all clusters and plots the convex hull for a visual check.
        """
        for data in self.cluster_data:
            pdb_file = data['pdb_file']
            cluster_size = data['cluster_size']
            pdb_handler = PDBFileHandler(pdb_file, core_residue_names=self.core_residue_names, 
                                         shell_residue_names=self.shell_residue_names)
            coordinates = np.array([atom.coordinates for atom in pdb_handler.core_atoms + pdb_handler.shell_atoms])
            if len(coordinates) < 4:
                print(f"Cluster size {cluster_size} is too small for Convex Hull calculation.")
                continue

            hull = ConvexHull(coordinates)
            self.plot_convex_hull(coordinates, hull, cluster_size)

    # - Scattering Cross Section Method
    def obtain_crossections(self, atoms, energy=17000):
        """
        Calculate the coherent scattering cross-sections for each atom in the cluster.

        Parameters:
        - atoms: list of Atom objects, where each atom has 'element', 'coordinates'.
        - energy: float, x-ray energy in eV for calculating the scattering cross-section (default is 17000 eV).

        Returns:
        - elements: np.array, corresponding element symbols for each atom.
        - cross_sections: np.array, corresponding coherent scattering cross-section values for each atom.
        """
        elements = [atom.element for atom in atoms]
        
        # Use a set to avoid duplicate element lookups in xraydb
        unique_elements = list(set(elements))
        
        # Precompute the cross-sections for unique elements
        element_to_cross_section = {
            element: xraydb.coherent_cross_section_elam(element, energy)
            for element in unique_elements
        }
        
        cross_sections = np.array([element_to_cross_section[element] for element in elements])

        return np.array(elements), cross_sections

    def calculate_coherentscattering_volume(self, atoms, energy=17000.0):
        """
        Calculate the total coherent scattering volume of a cluster based on the interaction volume per atom.

        Parameters:
        - atoms: list of Atom objects, where each atom has 'element', 'coordinates'.
        - energy: float, x-ray energy in eV for calculating the scattering cross-section (default is 17000 eV).

        Returns:
        - cluster_volume: float, the estimated cluster volume in angstrom^3.
        """
        # Calculate elements and cross-sections
        elements, cross_sections = self.obtain_crossections(atoms, energy)

        # Use a set to avoid duplicate element lookups in xraydb
        unique_elements = list(set(elements))
        
        # Precompute atomic masses and convert to grams per atom
        element_to_grams_per_atom = {
            element: xraydb.atomic_mass(element) / 6.022e23  # grams per atom
            for element in unique_elements
        }

        # Convert cross-sections from cm²/gram to Å²/atom
        cross_sections_angstrom = np.array([
            cross_section * 1e16 * element_to_grams_per_atom[element]
            for element, cross_section in zip(elements, cross_sections)
        ])
        
        # Calculate interaction radii from cross-sections
        interaction_radii = np.sqrt(cross_sections_angstrom / np.pi)

        # Calculate volumes of spheres based on interaction radii
        interaction_volumes = (4/3) * np.pi * (interaction_radii**3)

        # Sum the interaction volumes to get the total cluster volume
        total_cluster_volume = np.sum(interaction_volumes)

        return total_cluster_volume

    # - Voronoi Polyhedral Construction Method 
    def fetch_ionic_radius(self, element_symbol):
        """
        Fetch the ionic radius of an element from the Mendeleev library.
        """
        elem = element(element_symbol)
        
        # Define the typical oxidation states for common elements
        oxidation_states = {
            'Pb': 2,   # Lead usually has a +2 oxidation state
            'I': -1,   # Iodine usually has a -1 oxidation state
            'S': -2,   # Sulfur typically has a -2 oxidation state
            'O': -2,   # Oxygen typically has a -2 oxidation state
            'H': 1,    # Hydrogen typically has a +1 oxidation state
            'C': 4,    # Carbon typically has a +4 oxidation state in organic molecules (can vary)
            'N': -3    # Nitrogen typically has a -3 oxidation state (can vary)
            # Add other elements as needed
        }
        
        # Fetch the appropriate oxidation state for the element
        oxidation_state = oxidation_states.get(element_symbol, None)
        
        if oxidation_state is None:
            raise ValueError(f"Unknown or unsupported element {element_symbol}")
        
        # Find the ionic radius that matches the oxidation state
        for ionic_radius in elem.ionic_radii:
            if ionic_radius.charge == oxidation_state:
                return ionic_radius.ionic_radius
        
        raise ValueError(f"No ionic radius found for element {element_symbol} with oxidation state {oxidation_state}")

    def calculate_geometric_center(self, centers):
        return np.mean(centers, axis=0)

    def generate_dodecahedron_vertices(self):
        phi = (1 + np.sqrt(5)) / 2  # Golden ratio
        vertices = np.array([
            [-1, -1, -1], [1, -1, -1], [-1, 1, -1], [1, 1, -1],
            [-1, -1, 1], [1, -1, 1], [-1, 1, 1], [1, 1, 1],
            [0, -1/phi, -phi], [0, 1/phi, -phi], [0, -1/phi, phi], [0, 1/phi, phi],
            [-1/phi, -phi, 0], [1/phi, -phi, 0], [-1/phi, phi, 0], [1/phi, phi, 0],
            [-phi, 0, -1/phi], [phi, 0, -1/phi], [-phi, 0, 1/phi], [phi, 0, 1/phi]
        ])
        return vertices / np.linalg.norm(vertices[0])

    def generate_outward_facing_points(self, position, radius, geometric_center):
        direction = position - geometric_center
        direction /= np.linalg.norm(direction)  # Normalize to unit length
        vertices = self.generate_dodecahedron_vertices()
        outward_facing_vertices = [vertex for vertex in vertices if np.dot(vertex, direction) > 0]
        surface_points = position + radius * np.array(outward_facing_vertices)
        return surface_points

    def estimate_connected_volume_with_outward_facing_points(self, centers, radii):
        geometric_center = self.calculate_geometric_center(centers)
        all_points = []
        for position, radius in zip(centers, radii):
            surface_points = self.generate_outward_facing_points(position, radius, geometric_center)
            all_points.append(surface_points)
        all_points = np.vstack(all_points)
        hull = ConvexHull(all_points)
        connected_volume = hull.volume
        return connected_volume, hull

    def calculate_voronoi_volume(self, atoms):
        centers = np.array([atom.coordinates for atom in atoms])
        radii = np.array([self.fetch_ionic_radius(atom.element) for atom in atoms])
        volume, hull = self.estimate_connected_volume_with_outward_facing_points(centers, radii)
        return volume
    
    ## -- Plotting Methods
    @staticmethod
    def custom_glossy_marker(ax, x, y, base_color, markersize=8, offset=(0.08, 0.08)):
        for (xi, yi) in zip(x, y):
            # Draw the base marker
            ax.plot(xi, yi, 'o', markersize=markersize, color=base_color, zorder=1)

            gloss_params = [
                (markersize * 0.008, 0.3),  # Largest circle, more transparent
                (markersize * 0.005, 0.6),  # Middle circle, less transparent
                (markersize * 0.002, 1.0)   # Smallest circle, no transparency
                ]
            # # Offset for the glossy effect
            # x_offset, y_offset = offset
        
            x_offset = markersize/20 * offset[0]
            y_offset = markersize/20 * offset[1]

            # Overlay glossy effect - smaller concentric circles as highlights
            for i, (size, alpha) in enumerate(gloss_params):
                circle = plt.Circle((xi - x_offset, yi + y_offset), size, color='white', alpha=alpha, transform=ax.transData, zorder=2+i)
                ax.add_patch(circle)

    def plot_convex_hull(self, coordinates, hull, cluster_size):
        """
        Plots the convex hull and the atomic coordinates of a cluster.

        Parameters:
        - coordinates: np.array, the atomic coordinates of the cluster.
        - hull: ConvexHull object, the convex hull of the cluster.
        - cluster_size: int, the size of the cluster.
        """
        fig = plt.figure()
        ax = fig.add_subplot(111, projection='3d')

        # Plotting the atomic coordinates
        ax.scatter(coordinates[:, 0], coordinates[:, 1], coordinates[:, 2], color='r', s=100)

        # Plotting the convex hull
        for simplex in hull.simplices:
            simplex = np.append(simplex, simplex[0])  # loop back to the first vertex
            ax.plot(coordinates[simplex, 0], coordinates[simplex, 1], coordinates[simplex, 2], 'k-')

        # Setting the title
        ax.set_title(f'Convex Hull Visualization for Cluster Size {cluster_size}')

        plt.show()

    def plot_cluster_size_distribution(self, all_cluster_sizes):
        unique_sizes, counts = np.unique(all_cluster_sizes, return_counts=True)

        plt.figure(figsize=(8, 6))
        plt.bar(unique_sizes, counts, color='blue', edgecolor='black')
        plt.xlabel(f'Cluster Size ({self.target_elements[0]} Atom Count)', fontsize = 14)
        plt.ylabel('Number of Clusters', fontsize = 14)
        plt.xticks(fontsize=12)
        plt.yticks(fontsize=12)
        plt.title('Histogram of Cluster Sizes')
        plt.grid(True)
        plt.show()

    def plot_coordination_histogram(self, coordination_stats_per_size, title=None):
        sizes = sorted(coordination_stats_per_size.keys())
        pairs = set(pair for data in coordination_stats_per_size.values() for pair in data.keys())

        # Calculate mean coordination numbers and standard deviations
        coord_data = {
            pair: [np.mean(coordination_stats_per_size[size].get(pair, [0])) for size in sizes] for pair in pairs
        }
        coord_stds = {
            pair: [np.std(coordination_stats_per_size[size].get(pair, [0])) for size in sizes] for pair in pairs
        }
        cluster_counts = [len(coordination_stats_per_size[size][next(iter(pairs))]) for size in sizes]

        # Calculate weighted averages and standard deviations for legend labels
        weighted_avgs = {}
        weighted_stds = {}
        for pair in pairs:
            weighted_sum = sum(coord_data[pair][i] * cluster_counts[i] for i in range(len(sizes)))
            total_clusters = sum(cluster_counts)
            weighted_avg = weighted_sum / total_clusters if total_clusters > 0 else 0
            weighted_avgs[pair] = weighted_avg
            weighted_stds[pair] = np.mean(coord_stds[pair])  # Simple average of standard deviations for the legend

        # Default title if not provided
        if title is None:
            title = f'{self.target_elements[0]} Coordination Number vs. Cluster Size'

        plt.figure(figsize=(10, 6))

        bottom = np.zeros(len(sizes))

        for pair in pairs:
            neighbor_element = pair[1]
            if neighbor_element == 'O':
                color = (1.0, 0, 0, 0.7)  # red with 70% transparency
            elif neighbor_element == 'I':
                color = (0.3, 0, 0.3, 0.7)  # purple with 70% transparency
            elif neighbor_element == 'S':
                color = (0.545, 0.545, 0, 0.7)  # dark yellow with 70% transparency
            else:
                color = (0.5, 0.5, 0.5, 0.7)  # gray as a fallback

            coord_values = np.array(coord_data[pair])
            std_values = np.array(coord_stds[pair])

            # Plot bars
            plt.bar(sizes, coord_values, bottom=bottom, color=color, edgecolor='black', linewidth=1,
                    label=f"{pair[0]} - {pair[1]} , CN: {weighted_avgs[pair]:.2f} ± {weighted_stds[pair]:.2f}")
            
            # Add error bars for standard deviations centered on the top of each box
            plt.errorbar(sizes, bottom + coord_values, yerr=std_values, fmt='none', ecolor='black', capsize=5)

            bottom += coord_values

        plt.axhline(y=5, color='gray', linestyle='--')  # Dashed line at y = 5
        plt.ylim(0, 6)  # Increase y-axis bound to 6
        
        # Increase font sizes
        plt.xlabel(f'Cluster Size ({self.target_elements[0]} Atom Count)', fontsize=14)
        plt.ylabel(f'{self.target_elements[0]} Coordination Number', fontsize=14)
        plt.xticks(fontsize=12)
        plt.yticks(fontsize=12)
        
        # Place legend in a box on the right
        plt.legend(frameon=True, fontsize=12, loc='upper right', bbox_to_anchor=(1, 1), edgecolor='black')

        plt.title(title, fontsize=16)
        plt.show()

    def plot_average_volume_vs_cluster_size(self):
        """
        Plots the average volume of clusters versus the cluster size with error bars representing the standard deviation.
        Uses custom glossy markers for the data points.
        """
        # Ensure the statistics are calculated
        if not hasattr(self, 'average_volumes_per_size'):
            self.generate_statistics()

        sizes = sorted(self.average_volumes_per_size.keys())
        avg_volumes = [self.average_volumes_per_size[size] for size in sizes]
        std_devs = [np.std(self.cluster_size_distribution[size]) for size in sizes]  # Calculate standard deviations

        fig, ax = plt.subplots(figsize=(10, 6))

        # Plot the error bars and the data points
        plt.errorbar(sizes, avg_volumes, yerr=std_devs, fmt='o-', color='blue', ecolor='black', capsize=5, label='Average Volume')
        plt.xlabel(f'Cluster Size ({self.target_elements[0]} Atom Count)', fontsize=14)
        plt.ylabel(r'$<V_{\mathrm{cluster}}> \ (\mathrm{\AA}^{3})$', fontsize=14)
        plt.title('Average Cluster Volume vs Cluster Size', fontsize=16)
        plt.xticks(fontsize=12)
        plt.yticks(fontsize=12)
        plt.grid(True)
        plt.legend(['Average Volume'], fontsize=12)
        plt.show()

    def plot_average_volume_vs_cluster_size_rg(self):
        """
        Plots the average volume of clusters versus the cluster size with error bars representing the standard deviation.
        This version of the plot specifically indicates that the volumes are calculated using the radius of gyration (R_g).
        """
        # Ensure the statistics are calculated
        if not hasattr(self, 'average_volumes_per_size'):
            self.generate_statistics()

        sizes = sorted(self.average_volumes_per_size.keys())
        avg_volumes = [self.average_volumes_per_size[size] for size in sizes]
        std_devs = [np.std(self.cluster_size_distribution[size]) for size in sizes]  # Calculate standard deviations

        fig, ax = plt.subplots(figsize=(10, 6))

        # Plot the error bars and the data points
        plt.errorbar(sizes, avg_volumes, yerr=std_devs, fmt='o-', color='blue', ecolor='black', capsize=5, label='Average Volume')
        plt.xlabel(f'Cluster Size ({self.target_elements[0]} Atom Count)', fontsize=14)
        plt.ylabel(r'$<V_{\mathrm{cluster}}> \ (\mathrm{\AA}^{3})$', fontsize=14)
        plt.title('Average Cluster Volume vs Cluster Size (Based on $R_g$)', fontsize=16)
        plt.xticks(fontsize=12)
        plt.yticks(fontsize=12)
        plt.grid(True)
        plt.legend(['Average Volume (Based on $R_g$)'], fontsize=12)
        plt.show()

    def plot_volume_percentage_of_scatterers(self, box_size_angstroms, num_boxes):
        """
        Plots the volume percentage of scatterers for each cluster size using the average cluster volume
        divided by the total volume of all clusters. Highlights the mode bar with a complementary color.
        The calculations for the median and mean cluster sizes are included but the labels are commented out.

        Parameters:
        - box_size_angstroms: float, the size of the box in angstroms.
        - num_boxes: int, the number of boxes containing clusters.
        """
        # Calculate the total volume of all clusters
        total_cluster_volume = 0.0
        for size, volumes in self.cluster_size_distribution.items():
            avg_volume = np.mean(volumes)
            total_cluster_volume += avg_volume * len(volumes)

        # Calculate the volume percentage for each cluster size
        sizes = sorted(self.cluster_size_distribution.keys())
        volume_percentages = []

        for size in sizes:
            if len(self.cluster_size_distribution[size]) > 0:
                avg_volume = np.mean(self.cluster_size_distribution[size])
                volume_percentage = (avg_volume * len(self.cluster_size_distribution[size]) / total_cluster_volume) * 100
                volume_percentages.append(volume_percentage)
            else:
                volume_percentages.append(0)

        # Calculate the mode of the volume percentage distribution
        mode_index = np.argmax(volume_percentages)
        mode_size = sizes[mode_index]
        mode_percentage = volume_percentages[mode_index]

        # Calculate the weighted median and mean of the cluster sizes
        weighted_cluster_sizes = []
        for size, percentage in zip(sizes, volume_percentages):
            weighted_cluster_sizes.extend([size] * int(percentage * 100))  # Weight by percentage

        median_size = np.median(weighted_cluster_sizes) if weighted_cluster_sizes else 0
        mean_size = np.mean(weighted_cluster_sizes) if weighted_cluster_sizes else 0

        # Plotting the volume percentage histogram
        plt.figure(figsize=(10, 6))
        
        # Highlight the mode cluster size bar with a complementary color
        bar_colors = ['green'] * len(sizes)
        bar_colors[mode_index] = 'orange'  # Highlight the mode bar

        plt.bar(sizes, volume_percentages, color=bar_colors, edgecolor='black')
        plt.xlabel(f'Cluster Size ({self.target_elements[0]} Atom Count)', fontsize=14)
        
        # Update the ylabel to the correct format
        # plt.ylabel(r'$\phi \times <V_{\mathrm{c}}> \ (\mathrm{\AA}^{3})$', fontsize=14)
        plt.ylabel(r'$\phi$ (Volume %)', fontsize=14)
        
        plt.title(f'% Scattering Contribution vs Cluster Size ({self.target_elements[0]} Atom Count)', fontsize=16)
        plt.xticks(fontsize=12)
        plt.yticks(fontsize=12)
        plt.grid(True)

        # Display the mode of the cluster size contributing to scattering
        mode_cluster_volume = np.mean(self.cluster_size_distribution[mode_size])
        mode_cluster_count = len(self.cluster_size_distribution[mode_size])
        mode_volume_percentage = (mode_cluster_volume * mode_cluster_count / total_cluster_volume) * 100

        annotation_text = (
            f'Mode: Cluster Size = {mode_size}, Total Cluster Volume % = {mode_volume_percentage:.2f}%' #\n'
            # f'Median Cluster Size = {median_size:.2f}\n'  # Commented out
            # f'Mean Cluster Size = {mean_size:.2f}'  # Commented out
        )
        plt.annotate(annotation_text, xy=(0.98, 0.98), xycoords='axes fraction', ha='right', va='top', fontsize=12,
                    bbox=dict(boxstyle="round,pad=0.3", edgecolor="black", facecolor="white"))

        plt.show()

    def plot_charge_vs_cluster_size(self):
        """
        Plots the average charge per cluster versus the cluster size.
        """
        sizes = [data['cluster_size'] for data in self.cluster_data]
        charges = [data['charge'] for data in self.cluster_data]

        plt.figure(figsize=(10, 6))
        plt.scatter(sizes, charges, color='blue', label='Charge/Cluster')
        plt.xlabel(f'Cluster Size ({self.target_elements[0]} Count)', fontsize=14)
        plt.ylabel(r'<Charge>/Cluster (e)', fontsize=14)
        plt.title('Average Charge per Cluster vs Cluster Size', fontsize=16)
        plt.grid(True)
        plt.legend(fontsize=12)
        plt.show()

    # def plot_coordination_histogram(self, coordination_stats_per_size, title=None):
        sizes = sorted(coordination_stats_per_size.keys())
        pairs = set(pair for data in coordination_stats_per_size.values() for pair in data.keys())

        coord_data = {pair: [np.mean(coordination_stats_per_size[size].get(pair, [0])) for size in sizes] for pair in pairs}
        cluster_counts = [len(coordination_stats_per_size[size][next(iter(pairs))]) for size in sizes]

        # Calculate weighted averages
        weighted_avgs = {}
        for pair in pairs:
            weighted_sum = sum(coord_data[pair][i] * cluster_counts[i] for i in range(len(sizes)))
            total_clusters = sum(cluster_counts)
            weighted_avgs[pair] = weighted_sum / total_clusters

        # Default title if not provided
        if title is None:
            title = f'{self.target_elements[0]} Coordination Number v. Cluster Size'

        plt.figure(figsize=(10, 6))

        bottom = np.zeros(len(sizes))

        for pair in pairs:
            neighbor_element = pair[1]
            if neighbor_element == 'O':
                color = (1.0, 0, 0, 0.7)  # red with 50% transparency
            elif neighbor_element == 'I':
                color = (0.3, 0, 0.3, 0.7)  # purple with 50% transparency
            elif neighbor_element == 'S':
                color = (0.545, 0.545, 0, 0.7)  # dark yellow with 50% transparency
            else:
                color = (0.5, 0.5, 0.5, 0.7)  # gray as a fallback

            coord_values = np.array(coord_data[pair])
            plt.bar(sizes, coord_values, bottom=bottom, color=color, edgecolor='black', linewidth=1, label=f"{pair[0]} - {pair[1]}")
            bottom += coord_values

        plt.axhline(y=5, color='gray', linestyle='--')  # Dashed line at y = 5
        plt.ylim(0, 6.5)  # Increase y-axis bound to 6
        
        # Increase font sizes
        plt.xlabel(f'Cluster Size ({self.target_elements[0]} Atom Count)', fontsize=14)
        plt.ylabel(f'{self.target_elements[0]} Coordination Number', fontsize=14)
        plt.xticks(fontsize=12)
        plt.yticks(fontsize=12)
        
        # Place legend in a box on the right
        plt.legend(frameon=True, fontsize=12, loc='upper right', bbox_to_anchor=(1, 1), edgecolor='black')

        # Add annotation with weighted averages in the top-left
        annotation_text = 'Average Coordination Numbers:\n' + '\n'.join([f"{pair[0]} - {pair[1]}: {weighted_avgs[pair]:.2f}" for pair in pairs])
        plt.annotate(annotation_text, xy=(0.02, 0.98), xycoords='axes fraction', ha='left', va='top', fontsize=12,
                    bbox=dict(boxstyle="round,pad=0.3", edgecolor="black", facecolor="white"))

        plt.title(title, fontsize=16)
        plt.show()

    def plot_phi_Vc_vs_cluster_size(self, box_size_angstroms=None, num_boxes=None):
        """
        Plots the product of the volume fraction and the average cluster volume for each cluster size.
        The y-axis represents phi * <V_c>, where phi is the volume fraction and <V_c> is the average cluster volume.
        
        Parameters:
        - box_size_angstroms: float, the size of the box in angstroms.
        - num_boxes: int, the number of boxes containing clusters.
        """
        # Calculate the total volume of all clusters
        total_cluster_volume = 0.0
        for size, volumes in self.cluster_size_distribution.items():
            avg_volume = np.mean(volumes)
            total_cluster_volume += avg_volume * len(volumes)

        # Calculate the product of volume fraction and average volume for each cluster size
        sizes = sorted(self.cluster_size_distribution.keys())
        phi_Vc_values = []

        for size in sizes:
            if len(self.cluster_size_distribution[size]) > 0:
                avg_volume = np.mean(self.cluster_size_distribution[size])
                volume_fraction = (avg_volume * len(self.cluster_size_distribution[size]) / total_cluster_volume)
                phi_Vc = avg_volume * volume_fraction
                phi_Vc_values.append(phi_Vc)
            else:
                phi_Vc_values.append(0)

        # Calculate the mode of the phi * <V_c> distribution
        mode_index = np.argmax(phi_Vc_values)
        mode_size = sizes[mode_index]
        mode_phi_Vc = phi_Vc_values[mode_index]

        # Plotting the phi * <V_c> histogram
        plt.figure(figsize=(10, 6))
        
        # Highlight the mode cluster size bar with a complementary color
        bar_colors = ['green'] * len(sizes)
        bar_colors[mode_index] = 'orange'  # Highlight the mode bar

        plt.bar(sizes, phi_Vc_values, color=bar_colors, edgecolor='black')
        plt.xlabel(f'Cluster Size ({self.target_elements[0]} Atom Count)', fontsize=14)
        
        # Set the ylabel to the combined format
        plt.ylabel(r'$\phi \times <V_{\mathrm{c}}> \ (\mathrm{\AA}^{3})$', fontsize=14)
        
        plt.title(r'$\phi \times <V_{\mathrm{c}}>$ vs Cluster Size', fontsize=16)
        plt.xticks(fontsize=12)
        plt.yticks(fontsize=12)
        plt.grid(True)

        # Display the mode of the cluster size contributing to phi * <V_c>
        annotation_text = (
            f'Mode: Cluster Size = {mode_size}' #, phi * <V_c> = {mode_phi_Vc:.2f} $\mathrm{{\AA}}^3$'
        )
        plt.annotate(annotation_text, xy=(0.98, 0.98), xycoords='axes fraction', ha='right', va='top', fontsize=12,
                    bbox=dict(boxstyle="round,pad=0.3", edgecolor="black", facecolor="white"))

        plt.show()

    ## -- SAXS Calculations
    def calculate_total_iq(self, q_values, shape_type='sphere'):
        """
        Calculate the total scattering intensity I(q) for the polydisperse distribution of clusters.
        This is done as a weighted average of I(q) values, where the weights are the number of clusters of each size.

        Parameters:
        - q_values: A numpy array of q-values in inverse angstroms.
        - shape_type: 'sphere' or 'ellipsoid' to choose the volume calculation method.
        
        Returns:
        - total_iq: A numpy array of weighted average I(q) values.
        """
        total_iq = np.zeros_like(q_values)
        total_clusters = 0
        
        for data in self.cluster_data:
            # Retrieve the volume and scattering dimensions based on the cluster shape
            if shape_type == 'sphere':
                volume = data['volume']
                sphere_scattering = SphereScattering(volume=volume)
                iq_values = sphere_scattering.calculate_iq(q_values)
            elif shape_type == 'ellipsoid':
                Rgx = data['Rgx']
                Rgy = data['Rgy']
                Rgz = data['Rgz']
                ellipsoid_scattering = EllipsoidScattering(a=Rgx, b=Rgy, c=Rgz)
                iq_values = ellipsoid_scattering.calculate_iq(q_values)
            else:
                raise ValueError(f"Unknown shape type: {shape_type}")

            # Weight I(q) by the number of clusters of this size
            num_clusters = len(self.cluster_size_distribution[data['cluster_size']])
            weighted_iq = iq_values * num_clusters
            # Add to the total I(q)
            total_iq += weighted_iq
            # Accumulate the total number of clusters
            total_clusters += num_clusters

        # Normalize by the total number of clusters to get the weighted average
        total_iq /= total_clusters
        
        return total_iq

    def plot_total_iq(self, q_values):
        """
        Plot the total I(q) vs. q on a log-log scale.
        
        Parameters:
        - q_values: A numpy array of q-values in inverse angstroms.
        """
        total_iq = self.calculate_total_iq(q_values)
        
        # Create the plot
        plt.figure(figsize=(8, 6))
        plt.loglog(q_values, total_iq, marker='o', linestyle='-', color='r')
        plt.xlabel('q (Å⁻¹)')
        plt.ylabel('I(q)')
        plt.title('Total Scattering Intensity I(q) vs. Scattering Vector q')
        plt.grid(True, which="both", ls="--")
        plt.show()

    def save_total_iq(self, q_values, sample_name="sample"):
        """
        Save the total I(q) vs. q data to a .txt file.
        
        Parameters:
        - q_values: A numpy array of q-values in inverse angstroms.
        - sample_name: A string prefix for the filename.
        """
        total_iq = self.calculate_total_iq(q_values)
        
        # Get the current timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Create the filename with the sample name and timestamp
        filename = f"{sample_name}_IQ_{timestamp}.txt"
        
        # Save the data to the file
        data = np.column_stack((q_values, total_iq))
        np.savetxt(filename, data, header="q (Å⁻¹)\tI(q)", fmt="%.6e", delimiter="\t")
        
        print(f"Total I(q) saved to {filename}")
        